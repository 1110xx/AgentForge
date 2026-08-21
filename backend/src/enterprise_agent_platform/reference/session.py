"""Deterministic in-memory RunSessionProvider for the reference vertical."""
from __future__ import annotations

from dataclasses import dataclass, field

from enterprise_agent_platform.execution.session import (
    FollowupExchange,
    SessionHandle,
    SessionProviderError,
)


@dataclass(slots=True)
class _Session:
    run_id: str
    intent: str
    history: list[FollowupExchange] = field(default_factory=list)


class InMemoryRunSessionProvider:
    """Reference-only session provider that is reproducible and deterministic.

    The session itself is the memory: each follow-up answer is derived from the
    session's stored intent and turn count, proving that no external context is
    re-assembled per call.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._closed: set[str] = set()

    async def open(
        self,
        *,
        run_id: str,
        intent: str,
        resource_refs: tuple[str, ...],
        host_context_ref: str | None,
    ) -> SessionHandle:
        del resource_refs, host_context_ref
        session_id = f"session:{run_id}"
        if session_id in self._sessions and session_id not in self._closed:
            raise SessionProviderError("SESSION_ALREADY_OPEN", "one session per Run")
        # Clear the closed flag so _require_open works with the new session
        self._closed.discard(session_id)
        self._sessions[session_id] = _Session(run_id=run_id, intent=intent)
        return SessionHandle(session_id=session_id, run_id=run_id)

    async def run_task(self, handle: SessionHandle) -> None:
        self._require_open(handle)

    async def followup(
        self,
        handle: SessionHandle,
        message: str,
        *,
        read_only: bool = True,
    ) -> str:
        del read_only  # the reference stub performs no writes regardless
        session = self._require_open(handle)
        answer = f"[{session.intent}] 追问#{len(session.history) + 1}：{message}"
        session.history.append(FollowupExchange(question=message, answer=answer))
        return answer

    async def close(self, handle: SessionHandle) -> None:
        self._require_open(handle)
        self._closed.add(handle.session_id)

    def _require_open(self, handle: SessionHandle) -> _Session:
        if handle.session_id not in self._sessions:
            raise SessionProviderError("SESSION_NOT_FOUND", "session was not found")
        if handle.session_id in self._closed:
            raise SessionProviderError("SESSION_CLOSED", "session is closed")
        return self._sessions[handle.session_id]


__all__ = ["InMemoryRunSessionProvider"]
