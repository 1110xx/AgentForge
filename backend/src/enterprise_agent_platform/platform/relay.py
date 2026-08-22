"""Outbox relay + wake-up consumer — the missing NATS wiring (Phase 3).

The durable write side is complete (every state-changing operation appends an
Outbox row in the same transaction) and the components are complete
(``OutboxPublisher`` at-least-once CAS publish, ``InboxConsumer`` transactional
dedup, ``NatsJetStreamBus``). What was missing is the resident loop:

1. ``OutboxRelay`` — a continuous loop that drains ``PENDING`` outbox rows to
   the bus and backoff while there is nothing to send.
2. ``WakeupConsumer`` — subscribes to the scheduler wake-up subjects
   (``dispatch.requested`` / ``attempt.provisioning.requested`` /
   ``scheduler.work.ready``), records the delivery in the Inbox (transactional
   dedup) and kicks the local scheduler to claim immediately instead of
   waiting for the next poll cycle.

The wake-up path is an optimized side channel: ``FairScheduler`` still polls
the store every ``poll_interval`` and remains the single source of admission;
NATS only reduces claim latency and provides cross-replica wake signaling.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from enterprise_agent_platform.persistence.protocol import PlatformStore

from .message_bus import InboxConsumer, MessageBus, MessageEnvelope
from .outbox import OutboxPublishBatch, OutboxPublisher

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OutboxRelay:
    """Resident outbox → bus relay loop with adaptive backoff.

    After every ``run_once`` with selected rows the loop retries immediately
    (drain the backlog); when there is nothing pending it sleeps, doubling up
    to ``backoff_max_seconds`` so an idle relay does not hot-spin the store.
    """

    store: PlatformStore
    bus: MessageBus | None = None
    publisher: OutboxPublisher | None = None
    limit: int = 100
    poll_interval: float = 0.5
    backoff_max_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 1000:
            raise ValueError("relay limit must be between 1 and 1000")
        if self.poll_interval <= 0 or self.backoff_max_seconds < self.poll_interval:
            raise ValueError("relay intervals are invalid")
        if self.publisher is None:
            if self.bus is None:
                raise ValueError("OutboxRelay requires a bus or a publisher")
            object.__setattr__(
                self, "publisher", OutboxPublisher(store=self.store, bus=self.bus)
            )

    async def run_once(self) -> OutboxPublishBatch:
        return await self.publisher.run_once(limit=self.limit)  # type: ignore[union-attr]

    async def run_loop(self) -> None:
        """Relay until cancelled; backoff doubles when the queue is empty."""
        assert self.publisher is not None
        backoff = self.poll_interval
        try:
            while True:
                batch = await self.publisher.run_once(limit=self.limit)
                if batch.selected:
                    logger.info(
                        "relay drained outbox: selected=%d published=%d deferred=%d failed=%d",
                        batch.selected,
                        batch.published,
                        batch.deferred,
                        batch.failed,
                    )
                    backoff = self.poll_interval
                    continue
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.backoff_max_seconds)
        except asyncio.CancelledError:
            logger.info("outbox relay stopped")


# Wake-up subjects published by the Control Plane when new schedulable work
# may exist. The consumer treats every delivery the same: record into the
# Inbox (dedup) and kick the local scheduler — admission itself stays with
# FairScheduler's poll + CAS claim.
WAKE_UP_SUBJECTS: tuple[str, ...] = (
    "dispatch.requested",
    "attempt.provisioning.requested",
    "scheduler.work.ready",
)


@dataclass(slots=True)
class WakeupConsumer:
    """Pull wake-up subjects from the bus and kick the scheduler.

    ``wake`` is a plain (possibly sync) callback — usually
    ``SchedulerService.wake``. The transactional side effect of each delivery
    is only the Inbox marker (claim → handler → PROCESSED), so a message is
    applied exactly once per handler version; the kick itself is idempotent
    (extra kicks merely trigger an empty claim attempt).
    """

    store: PlatformStore
    bus: MessageBus
    subjects: tuple[str, ...] = WAKE_UP_SUBJECTS
    handler_version: str = "scheduler-wake/v1"
    pull_timeout: float = 10.0
    wake: Callable[[], None] | None = None
    consumer_name: str = "agent-platform-wake"

    def __post_init__(self) -> None:
        if not self.subjects:
            raise ValueError("wake-up subjects must not be empty")
        if self.pull_timeout <= 0:
            raise ValueError("wake-up pull timeout must be positive")

    def _handler(self, _tx: Any, _envelope: MessageEnvelope) -> Awaitable[None]:
        async def apply(_tx: Any, _envelope: MessageEnvelope) -> None:
            if self.wake is not None:
                self.wake()

        return apply(_tx, _envelope)

    async def consume_loop(self) -> None:
        """Consume wake-up subjects until cancelled."""
        inbox = InboxConsumer(store=self.store, handler_version=self.handler_version)
        try:
            while True:
                for subject in self.subjects:
                    delivery = await self.bus.pull(
                        subject, consumer=self.consumer_name, timeout=self.pull_timeout
                    )
                    if delivery is None:
                        continue
                    try:
                        processed = await inbox.consume(delivery, self._handler)
                    except Exception:
                        logger.exception("wake-up consumer failed for %s", subject)
                        continue
                    if processed:
                        logger.info(
                            "wake-up applied: topic=%s message=%s",
                            subject,
                            delivery.envelope.message_id,
                        )
        except asyncio.CancelledError:
            logger.info("wake-up consumer stopped")


def create_nats_message_bus(
    *,
    servers: tuple[str, ...],
    stream: str,
    subjects: tuple[str, ...],
    replicas: int = 3,
) -> MessageBus:
    """Build the NATS JetStream bus adapter (lazy connect + stream ensure)."""
    from enterprise_agent_platform.platform.message_bus import NatsJetStreamBus

    return NatsJetStreamBus(
        servers=servers,
        stream=stream,
        subjects=subjects,
        replicas=replicas,
    )


@dataclass(slots=True)
class RelayServices:
    """Relay + wake-up consumer pair sharing one message bus."""

    relay: OutboxRelay
    consumer: WakeupConsumer
    bus: MessageBus

    async def run_forever(self) -> None:
        """Run relay and consumer concurrently until cancelled."""
        relay_task = asyncio.create_task(self.relay.run_loop())
        consumer_task = asyncio.create_task(self.consumer.consume_loop())
        try:
            await asyncio.gather(relay_task, consumer_task)
        finally:
            relay_task.cancel()
            consumer_task.cancel()
            await self.bus.close()


def create_relay_from_env(
    store: PlatformStore,
    *,
    wake: Callable[[], None] | None = None,
    subject_prefix: str = "agent",
) -> RelayServices | None:
    """Assemble NATS relay services from the platform env contract.

    Env:
    * ``AGENT_PLATFORM_NATS_URL`` — comma-separated server list (required to
      enable; absent or empty disables the relay, keeping local runs portable)
    * ``AGENT_PLATFORM_NATS_STREAM`` — JetStream stream name (default ``AGENT_PLATFORM``)
    * ``AGENT_PLATFORM_NATS_STREAM_REPLICAS`` — JetStream replica count (default 3)
    * ``AGENT_PLATFORM_NATS_SUBJECT_PREFIX`` — one NATS token (default ``agent``)

    The wake-up consumer subscribes to ``{prefix}.dispatch.requested`` etc.
    (the exact subjects ``OutboxPublisher`` publishes under), so a relay and a
    consumer in different processes still talk to the same stream.
    """
    import os

    raw_servers = os.environ.get("AGENT_PLATFORM_NATS_URL", "").strip()
    if not raw_servers:
        return None
    servers = tuple(
        server.strip() for server in raw_servers.split(",") if server.strip()
    )
    if not servers:
        return None
    stream = os.environ.get("AGENT_PLATFORM_NATS_STREAM", "AGENT_PLATFORM").strip()
    replicas_raw = os.environ.get("AGENT_PLATFORM_NATS_STREAM_REPLICAS", "3").strip()
    replicas = int(replicas_raw) if replicas_raw.isdigit() else 3
    prefix = os.environ.get(
        "AGENT_PLATFORM_NATS_SUBJECT_PREFIX", subject_prefix
    ).strip()
    if not prefix or "." in prefix:
        raise ValueError("AGENT_PLATFORM_NATS_SUBJECT_PREFIX must be one NATS token")

    stream_subjects = (f"{prefix}.>",)
    wake_subjects = tuple(f"{prefix}.{subject}" for subject in WAKE_UP_SUBJECTS)
    bus = create_nats_message_bus(
        servers=servers,
        stream=stream,
        subjects=stream_subjects,
        replicas=replicas,
    )
    relay = OutboxRelay(store=store, bus=bus)
    consumer = WakeupConsumer(
        store=store,
        bus=bus,
        subjects=wake_subjects,
        wake=wake,
    )
    return RelayServices(relay=relay, consumer=consumer, bus=bus)


__all__ = [
    "WAKE_UP_SUBJECTS",
    "OutboxRelay",
    "RelayServices",
    "WakeupConsumer",
    "create_nats_message_bus",
    "create_relay_from_env",
]


__all__ = [
    "WAKE_UP_SUBJECTS",
    "OutboxRelay",
    "WakeupConsumer",
    "create_nats_message_bus",
]