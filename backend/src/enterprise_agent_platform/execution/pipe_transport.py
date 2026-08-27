"""JSON-line pipe transport between orchestrator (parent) and runtime (child).

Phase-1 local development transport: no Docker, no network I/O. The parent
(``SubprocessOrchestrator``) spawns one child Python process per Attempt and
speaks a tiny request/response protocol over stdin/stdout::

    child  →  parent   {"id": N, "op": "...", "kwargs": {...}}
    parent →  child    {"id": N, "ok": true, "result": {...}}
                       {"id": N, "ok": false, "error": {"code": "...", "message": "..."}}

Windows note: ``asyncio`` Proactor cannot wrap ``sys.stdin``/``sys.stdout`` in
pipe transports (``connect_read_pipe``/``connect_write_pipe`` raise WinError 6
for console/pipe fds), so the child reads and writes at the raw fd level via
``os.read``/``os.write`` wrapped in ``asyncio.to_thread`` — one blocking reader
thread per child, which is fine for local development.

The child must never write application logs to stdout — logs go to stderr so
the protocol stream stays clean JSON lines.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

# ── Operation names (mirrors fastapi/internal.py runtime operations) ──
OP_BOOTSTRAP = "bootstrap"
OP_RESTORE = "restore"
OP_HEARTBEAT = "heartbeat"
OP_READ_TOOL = "read_tool"
OP_PUBLISH_ARTIFACT = "publish_artifact"
OP_PROPOSE_ACTION = "propose_action"
OP_MODEL_CALL = "model_call"
OP_COMMIT_CHECKPOINT = "commit_checkpoint"
OP_COMMIT_FINAL = "commit_final_checkpoint"
OP_RECORD_FAILURE = "record_failure"
# Live-streaming bridge ops (SDD §11.4/§11.5):
#   emit_event   — child -> parent, durable platform event (tool.execution.started/
#                  ended, agent.turn.completed) appended to the event log via
#                  append_event (Outbox -> SSE as regular `event` frames).
#   stream_chunk — child -> parent fire-and-forget (id=0, no reply expected):
#                  ephemeral ToolExecutionUpdate/StreamThinking/StreamText deltas
#                  that are ONLY forwarded to the in-memory chunk relay and SSE
#                  as `stream-chunk` frames; never persisted, dropped on
#                  disconnect (SDD §11.5).
OP_EMIT_EVENT = "emit_event"
OP_STREAM_CHUNK = "stream_chunk"


class PipeError(RuntimeError):
    """A control-plane error surfaced across the pipe (mirrors PlatformError)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class PipeClient:
    """One child-side client: sends requests, awaits matched responses.

    A single background reader task matches ``id`` in the response stream.
    """

    def __init__(
        self,
        stdin_fd: int | None = None,
        stdout_fd: int | None = None,
    ) -> None:
        self._in_fd = stdin_fd if stdin_fd is not None else sys.stdin.fileno()
        self._out_fd = stdout_fd if stdout_fd is not None else sys.stdout.fileno()
        self._buffer = bytearray()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        self._write_lock: asyncio.Lock | None = None

    async def start(self) -> None:
        """Launch the reader loop (fd-level, Windows-safe)."""
        self._write_lock = asyncio.Lock()
        self._reader_task = asyncio.create_task(self._read_loop())

    async def request(self, op: str, **kwargs: Any) -> dict[str, Any]:
        """Send one request and await the matched response."""
        if self._closed:
            raise PipeError("PIPE_CLOSED", "pipe transport is closed")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        line = json.dumps(
            {"id": request_id, "op": op, "kwargs": kwargs},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await self._write_line(line.encode("utf-8") + b"\n")
        response = await future
        if not response.get("ok"):
            error = response.get("error") or {}
            raise PipeError(
                error.get("code", "PIPE_ERROR"),
                error.get("message", "control-plane request failed"),
            )
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def send_notify(self, op: str, **kwargs: Any) -> None:
        """Fire-and-forget one-way frame (``id: 0``, no reply awaited).

        Used by the live-streaming bridge for ephemeral ``stream_chunk``
        frames. The parent never replies to ``id: 0`` frames; a lost chunk is
        simply dropped (SDD §11.5 — ephemeral by design, replay comes from
        the durable ``agent.turn.completed`` event instead).
        """
        if self._closed:
            raise PipeError("PIPE_CLOSED", "pipe transport is closed")
        line = json.dumps(
            {"id": 0, "op": op, "kwargs": kwargs},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await self._write_line(line.encode("utf-8") + b"\n")

    async def _write_line(self, data: bytes) -> None:
        """Serialize writes so request frames and notify frames never interleave."""
        async with self._write_lock:
            await asyncio.to_thread(os.write, self._out_fd, data)

    async def _read_line(self) -> bytes | None:
        """Read one JSON line (fd-level, tolerant of partial reads)."""
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                line = bytes(self._buffer[: index + 1])
                del self._buffer[: index + 1]
                return line
            chunk = await asyncio.to_thread(os.read, self._in_fd, 65536)
            if not chunk:
                if self._buffer:
                    line = bytes(self._buffer)
                    self._buffer.clear()
                    return line
                return None
            self._buffer.extend(chunk)

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                raw = await self._read_line()
                if raw is None:
                    break
                try:
                    response = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    # A stray non-protocol line (logs go to stderr, so this
                    # should never happen in practice).
                    continue
                request_id = response.get("id")
                future = self._pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(response)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        PipeError("PIPE_CLOSED", "runtime pipe closed before response")
                    )
            self._pending.clear()

    async def close(self) -> None:
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass


def error_response(request_id: int, code: str, message: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def ok_response(request_id: int, result: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": request_id, "ok": True, "result": result or {}}