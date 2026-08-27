"""Replay-safe SSE framing with bounded batches and notifier-only wakeups."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol, runtime_checkable

from fastapi import Request

from enterprise_agent_platform.contracts.errors import ApiErrorEnvelope
from enterprise_agent_platform.contracts.events import EnterpriseEventEnvelope
from enterprise_agent_platform.control.views import RunQueryService
from enterprise_agent_platform.persistence.protocol import PlatformError
from enterprise_agent_platform.platform.run_chunks import RunChunkSource


@runtime_checkable
class RunEventSubscription(Protocol):
    async def wait(self, timeout_seconds: float) -> bool: ...


@runtime_checkable
class RunEventNotifier(Protocol):
    def subscribe(
        self, tenant_id: str, run_id: str
    ) -> AbstractAsyncContextManager[RunEventSubscription]: ...


class _PollingSubscription:
    async def wait(self, timeout_seconds: float) -> bool:
        await asyncio.sleep(timeout_seconds)
        return False


class PollingRunEventNotifier:
    @asynccontextmanager
    async def subscribe(
        self, tenant_id: str, run_id: str
    ) -> AsyncIterator[RunEventSubscription]:
        del tenant_id, run_id
        yield _PollingSubscription()


def event_frame(event: EnterpriseEventEnvelope) -> str:
    data = json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"id: {event.event_seq}\nevent: {event.event_type.value}\ndata: {data}\n\n"


def chunk_frame(chunk: dict[str, object]) -> str:
    """SSE frame for one ephemeral ``stream-chunk`` (SDD §11.5).

    Unlike ``event_frame`` this carries no ``id``/event_seq: stream chunks are
    not durable events, are not replayed after a reconnect and are dropped on
    disconnect. The frontend renders them for the live view only; persistence
    and replay come from the durable ``agent.turn.completed`` ``event`` frame.
    """
    data = json.dumps(
        chunk,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"event: stream-chunk\ndata: {data}\n\n"


def resync_required_frame(trace_id: str | None) -> str:
    envelope = ApiErrorEnvelope(
        schema_version="api-error/v1",
        code="RESYNC_REQUIRED",
        message="event cursor precedes retention floor",
        trace_id=trace_id,
    )
    data = json.dumps(
        envelope.model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"event: platform.resync-required\ndata: {data}\n\n"


async def stream_run_events(
    *,
    request: Request,
    query: RunQueryService,
    notifier: RunEventNotifier,
    tenant_id: str,
    run_id: str,
    after_event_seq: int,
    trace_id: str | None,
    heartbeat_seconds: float,
    max_lifetime_seconds: float,
    batch_size: int = 100,
    chunks: RunChunkSource | None = None,
    chunk_batch_size: int = 100,
) -> AsyncIterator[str]:
    if heartbeat_seconds <= 0 or max_lifetime_seconds <= 0:
        raise ValueError("SSE heartbeat and maximum lifetime must be positive")
    if not 1 <= batch_size <= 100:
        raise ValueError("SSE batch size must be between 1 and 100")
    cursor = after_event_seq
    deadline = time.monotonic() + max_lifetime_seconds
    async with notifier.subscribe(tenant_id, run_id) as subscription:
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                return
            # Ephemeral link: drain live stream chunks before the durable page
            # (SDD §11.5). A chunk batch is bounded; anything still queued stays
            # for the next drain and is dropped once the connection closes.
            if chunks is not None:
                for chunk in chunks.drain(run_id, limit=chunk_batch_size):
                    if time.monotonic() >= deadline or await request.is_disconnected():
                        return
                    yield chunk_frame(chunk)
            try:
                page = await query.get_events(
                    tenant_id,
                    run_id,
                    after_event_seq=cursor,
                    limit=batch_size,
                )
            except PlatformError as error:
                if error.code == "RESYNC_REQUIRED":
                    yield resync_required_frame(trace_id)
                    return
                raise
            if page.events:
                for event in page.events:
                    if time.monotonic() >= deadline or await request.is_disconnected():
                        return
                    yield event_frame(event)
                    cursor = event.event_seq
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            timeout = min(heartbeat_seconds, remaining)
            try:
                notified = await asyncio.wait_for(
                    subscription.wait(timeout), timeout=timeout + 0.05
                )
            except TimeoutError:
                notified = False
            if not notified:
                yield ":heartbeat\n\n"
