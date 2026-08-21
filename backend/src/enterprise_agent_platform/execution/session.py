"""Run-scoped model provider session seam.

One Run maps to one model provider session. The session is the agent's
"memory": the task runs inside the session, and after the Effect completes,
follow-up questions continue in the same session. The provider is host-injected
and long-lived -- it must outlive the per-Attempt Sandbox Pod, which is why it
is a platform-side Port rather than an in-Pod adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class SessionProviderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class SessionHandle:
    """Opaque handle identifying one provider session bound to a Run."""

    session_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class FollowupExchange:
    """One question/answer turn appended inside a Run's session."""

    question: str
    answer: str


@runtime_checkable
class RunSessionProvider(Protocol):
    """Host-injected, stateful model provider: one session per Run.

    This is the "agent brain" seam. Unlike the stateless ``AgentProvider`` in
    ``runtime.py``, the session provider holds the conversation natively, so the
    task and any follow-up questions share the same model memory.
    """

    async def open(
        self,
        *,
        run_id: str,
        intent: str,
        resource_refs: tuple[str, ...],
        host_context_ref: str | None,
    ) -> SessionHandle:
        """Open (or return) the session bound to this Run."""

    async def run_task(self, handle: SessionHandle) -> None:
        """Drive the task loop inside the session.

        Phase 0 keeps this as a seam only: the reference implementation is a
        no-op and the existing attempt-scoped execution chain remains unchanged.
        """

    async def followup(
        self,
        handle: SessionHandle,
        message: str,
        *,
        read_only: bool = True,
    ) -> str:
        """Append a user message to the session and return the model's answer."""

    async def close(self, handle: SessionHandle) -> None:
        """Close the session bound to this Run."""


__all__ = [
    "FollowupExchange",
    "RunSessionProvider",
    "SessionHandle",
    "SessionProviderError",
]
