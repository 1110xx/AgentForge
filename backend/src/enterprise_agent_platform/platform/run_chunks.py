"""Ephemeral per-run stream-chunk relay (SDD §11.5).

The relay is strictly in-memory: stream chunks are transient deltas
(ToolExecutionUpdate / StreamThinkingDelta / StreamTextDelta) that must never
be persisted. Producers (the Runtime child piping pi-agent-core events through
the orchestrator) ``push`` into a bounded per-run deque; the SSE endpoint
``drain``s the same per-run queue and frames each entry as a ``stream-chunk``
SSE frame. On disconnect the queued chunks are simply dropped — replay comes
from the durable ``agent.turn.completed`` event instead (SDD §11.4).

Deployment note: this relay is process-local. In the Phase-1 subprocess /
in-memory composition the worker and the API share the process, so the relay
works end to end. In a split api/worker deployment the chunks must travel over
an ephemeral transport (e.g. a NATS JetStream subject with short retention);
the platform-event link is unaffected because it is already durable.
"""
from __future__ import annotations

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


class InMemoryRunChunkRelay:
    """Bounded in-memory relay shared by the worker and the SSE endpoint."""

    def __init__(
        self,
        *,
        max_chunks_per_run: int = MAX_CHUNKS_PER_RUN,
        max_runs: int = MAX_RUNS,
    ) -> None:
        self._max_chunks_per_run = max_chunks_per_run
        self._max_runs = max_runs
        self._buffers: dict[str, deque[dict[str, Any]]] = {}

    def push(self, run_id: str, chunk: dict[str, Any]) -> None:
        buffer = self._buffers.get(run_id)
        if buffer is None:
            if len(self._buffers) >= self._max_runs:
                # Oldest run first (dict preserves insertion order).
                self._buffers.pop(next(iter(self._buffers)))
            buffer = deque()
            self._buffers[run_id] = buffer
        buffer.append(chunk)
        while len(buffer) > self._max_chunks_per_run:
            buffer.popleft()

    def drain(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        buffer = self._buffers.get(run_id)
        if buffer is None:
            return []
        out: list[dict[str, Any]] = []
        for _ in range(min(limit, len(buffer))):
            out.append(buffer.popleft())
        if not buffer:
            self._buffers.pop(run_id, None)
        return out

    def pending(self, run_id: str) -> int:
        buffer = self._buffers.get(run_id)
        return len(buffer) if buffer is not None else 0


__all__ = [
    "InMemoryRunChunkRelay",
    "RunChunkRelay",
    "RunChunkSink",
    "RunChunkSource",
]