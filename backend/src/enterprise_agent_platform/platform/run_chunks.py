"""Ephemeral per-run stream-chunk relay (SDD §11.5).

The relay is strictly in-memory: stream chunks are transient deltas
(ToolExecutionUpdate / StreamThinkingDelta / StreamTextDelta) that must never
be persisted. Producers (the Runtime child piping pi-agent-core events through
the orchestrator) ``push`` into a bounded per-run deque; the SSE endpoint
``drain``s the same per-run queue and frames each entry as a ``stream-chunk``
SSE frame. On disconnect the queued chunks are simply dropped — replay comes
from the durable ``agent.turn.completed`` event instead (SDD §11.4).

In addition to the passive ``push``/``drain`` surface the relay exposes an
optional push-driven wake-up (``wait_chunks``): a consumer waiting on
``asyncio.wait_for(event.wait(), ...)`` returns as soon as a producer pushes a
chunk, instead of sleeping until the next SSE heartbeat. This is what lets the
live view stream token deltas in near-real-time rather than in once-per-
heartbeat batches. ``wait_chunks`` is intentionally optional (duck-typed by the
SSE layer) so a split api/worker deployment can swap in an ephemeral transport
relay that implements only ``drain``.

Deployment note: this relay is process-local. In the Phase-1 subprocess /
in-memory composition the worker and the API share the process, so the relay
works end to end. In a split api/worker deployment the chunks must travel over
an ephemeral transport (e.g. a NATS JetStream subject with short retention);
the platform-event link is unaffected because it is already durable.
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any, Protocol

# Upper bound on queued chunks per run. The SSE drain is bounded by this and
# the chunk producer rate, but a slow/offline consumer must never grow memory
# without limit — older chunks are evicted (frontend live view loses them, the
# durable turn event remains the source of truth).
MAX_CHUNKS_PER_RUN = 500

# Upper bound on concurrent runs holding live chunk buffers; oldest run is
# evicted first (simple FIFO on the outer dict).
MAX_RUNS = 1000


class RunChunkSink(Protocol):
    def push(self, run_id: str, chunk: dict[str, Any]) -> None: ...


class RunChunkSource(Protocol):
    def drain(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]: ...


class RunChunkRelay(RunChunkSink, RunChunkSource, Protocol):
    """Combined producer/consumer view of the relay."""


def _running_loop_or_none() -> asyncio.AbstractEventLoop | None:
    """Current thread's running loop, if any (push may run off-loop)."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class InMemoryRunChunkRelay:
    """Bounded in-memory relay shared by the worker and the SSE endpoint.

    The relay is thread-safe (mutations are guarded by a lock) so producers can
    ``push`` from a worker event loop and the SSE consumer can ``drain`` from
    the API event loop even when the two run on different threads.
    """

    def __init__(
        self,
        *,
        max_chunks_per_run: int = MAX_CHUNKS_PER_RUN,
        max_runs: int = MAX_RUNS,
    ) -> None:
        self._max_chunks_per_run = max_chunks_per_run
        self._max_runs = max_runs
        self._buffers: dict[str, deque[dict[str, Any]]] = {}
        # Optional push-driven wake-up: a per-run asyncio.Event that ``push``
        # sets. Only created once an SSE consumer starts ``wait_chunks``; the
        # event is bound to (and signalled on) the consumer's event loop.
        self._wake_events: dict[str, asyncio.Event] = {}
        self._wake_loops: dict[str, asyncio.AbstractEventLoop] = {}
        self._lock = threading.RLock()

    def push(self, run_id: str, chunk: dict[str, Any]) -> None:
        event = None
        loop = None
        with self._lock:
            buffer = self._buffers.get(run_id)
            if buffer is None:
                if len(self._buffers) >= self._max_runs:
                    # Oldest run first (dict preserves insertion order).
                    evicted = next(iter(self._buffers))
                    self._buffers.pop(evicted)
                    self._wake_events.pop(evicted, None)
                    self._wake_loops.pop(evicted, None)
                buffer = deque()
                self._buffers[run_id] = buffer
            buffer.append(chunk)
            while len(buffer) > self._max_chunks_per_run:
                buffer.popleft()
            event = self._wake_events.get(run_id)
            loop = self._wake_loops.get(run_id)
        if event is None:
            return
        running = _running_loop_or_none()
        if loop is not None and loop is not running:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # Waiter loop already closed (SSE gone) — nothing to wake.
                pass
        else:
            event.set()

    def drain(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            buffer = self._buffers.get(run_id)
            if buffer is None:
                return []
            out: list[dict[str, Any]] = []
            for _ in range(min(limit, len(buffer))):
                out.append(buffer.popleft())
            if not buffer:
                self._buffers.pop(run_id, None)
                event = self._wake_events.get(run_id)
                if event is not None:
                    # Re-arm the wake signal once nothing is left queued, so the
                    # next push reliably wakes a sleeping consumer.
                    event.clear()
        return out

    def pending(self, run_id: str) -> int:
        with self._lock:
            buffer = self._buffers.get(run_id)
            return len(buffer) if buffer is not None else 0

    async def wait_chunks(self, run_id: str, timeout_seconds: float) -> bool:
        """Await a new chunk for ``run_id``; return True when one is queued.

        Resolves True immediately if chunks are already pending, then blocks up
        to ``timeout_seconds`` for a producer ``push``. Safe to call from the
        API event loop while producers push from another loop/thread.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            buffer = self._buffers.get(run_id)
            if buffer:
                event = self._wake_events.get(run_id)
                if event is not None:
                    event.clear()
                return True
            event = self._wake_events.get(run_id)
            if event is None:
                event = asyncio.Event()
                self._wake_events[run_id] = event
            if event.is_set():
                event.clear()
            self._wake_loops[run_id] = loop
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return False
        return True

    def discard_run(self, run_id: str) -> None:
        """Drop the run's buffer and wake state (SSE consumer disconnected)."""
        with self._lock:
            self._buffers.pop(run_id, None)
            self._wake_events.pop(run_id, None)
            self._wake_loops.pop(run_id, None)


__all__ = [
    "InMemoryRunChunkRelay",
    "RunChunkRelay",
    "RunChunkSink",
    "RunChunkSource",
]
