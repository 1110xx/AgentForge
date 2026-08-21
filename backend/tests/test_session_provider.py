"""Phase 0 unit tests: the RunSessionProvider session seam."""
from __future__ import annotations

import asyncio

import pytest

from enterprise_agent_platform.execution.session import (
    RunSessionProvider,
    SessionHandle,
    SessionProviderError,
)
from enterprise_agent_platform.reference.session import InMemoryRunSessionProvider


def _open(
    provider: InMemoryRunSessionProvider,
    *,
    run_id: str = "run-1",
    intent: str = "审批订单",
) -> SessionHandle:
    return asyncio.run(
        provider.open(
            run_id=run_id,
            intent=intent,
            resource_refs=("synthetic-case:1",),
            host_context_ref=None,
        )
    )


def _followup(
    provider: InMemoryRunSessionProvider,
    handle: SessionHandle,
    message: str,
) -> str:
    return asyncio.run(provider.followup(handle, message, read_only=True))


def test_one_session_per_run() -> None:
    provider = InMemoryRunSessionProvider()
    handle = _open(provider)
    assert handle.run_id == "run-1"
    with pytest.raises(SessionProviderError, match="one session per Run"):
        _open(provider)


def test_session_is_the_memory() -> None:
    provider = InMemoryRunSessionProvider()
    handle = _open(provider)
    first = _followup(provider, handle, "为什么拒绝？")
    second = _followup(provider, handle, "数据从哪来？")
    # Answers derive from the session's own state (intent + turn count),
    # proving the model memory lives inside the session, not re-assembled.
    assert "审批订单" in first and "追问#1" in first
    assert "追问#2" in second
    assert first != second


def test_close_then_followup_fails() -> None:
    provider = InMemoryRunSessionProvider()
    handle = _open(provider)
    asyncio.run(provider.close(handle))
    with pytest.raises(SessionProviderError, match="session is closed"):
        _followup(provider, handle, "还在吗？")


def test_followup_unknown_session_fails() -> None:
    provider = InMemoryRunSessionProvider()
    ghost = SessionHandle(session_id="session:missing", run_id="run-x")
    with pytest.raises(SessionProviderError, match="was not found"):
        _followup(provider, ghost, "你好")


def test_is_a_run_session_provider() -> None:
    assert isinstance(InMemoryRunSessionProvider(), RunSessionProvider)
