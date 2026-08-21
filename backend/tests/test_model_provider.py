"""Unit tests: the session-based reference model provider."""
from __future__ import annotations

import asyncio

import pytest

from enterprise_agent_platform.contracts.enums import EffectState, RunState
from enterprise_agent_platform.execution.session import (
    RunSessionProvider,
    SessionHandle,
    SessionProviderError,
)
from enterprise_agent_platform.reference.model_provider import ReferenceModelSessionProvider


def _run(provider: ReferenceModelSessionProvider, run_id: str = "demo-run-1") -> SessionHandle:
    handle = asyncio.run(
        provider.open(
            run_id=run_id,
            intent="分析合成失败数据集",
            resource_refs=("synthetic-dataset:reference",),
            host_context_ref=None,
        )
    )
    asyncio.run(provider.run_task(handle))
    return handle


def test_run_task_completes_vertical() -> None:
    provider = ReferenceModelSessionProvider()
    handle = _run(provider)
    summary = provider.task_summary(handle)
    assert RunState.SUCCEEDED.value in summary
    assert EffectState.SUCCEEDED.value in summary


def test_followup_answers_from_session_memory() -> None:
    provider = ReferenceModelSessionProvider()
    handle = _run(provider)
    why = asyncio.run(provider.followup(handle, "为什么判定需要创建缺陷单？"))
    assert "失败信号" in why  # reflects the actual analysis facts held in the session
    data = asyncio.run(provider.followup(handle, "查了哪些数据？"))
    assert "合成数据集" in data


def test_followup_before_run_task_fails() -> None:
    provider = ReferenceModelSessionProvider()
    handle = asyncio.run(
        provider.open(
            run_id="demo-2",
            intent="x",
            resource_refs=(),
            host_context_ref=None,
        )
    )
    with pytest.raises(SessionProviderError, match="run_task must complete"):
        asyncio.run(provider.followup(handle, "结果是什么？"))


def test_close_then_followup_fails() -> None:
    provider = ReferenceModelSessionProvider()
    handle = _run(provider)
    asyncio.run(provider.close(handle))
    with pytest.raises(SessionProviderError, match="closed"):
        asyncio.run(provider.followup(handle, "还在吗？"))


def test_is_a_run_session_provider() -> None:
    assert isinstance(ReferenceModelSessionProvider(), RunSessionProvider)
