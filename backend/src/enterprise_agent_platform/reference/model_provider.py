"""Reference session-based model provider for the local demo.

One session per Run. ``run_task`` drives the complete deterministic vertical
(read -> analyze -> propose -> approval -> Effect -> success) inside the session,
and ``followup`` answers from the session's stored task facts. This is the
demo's "model provider": a stateful brain that owns the session memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from enterprise_agent_platform.execution.session import (
    FollowupExchange,
    SessionHandle,
    SessionProviderError,
)
from enterprise_agent_platform.reference.provider import (
    CompletedReferenceRun,
    PausedReferenceRun,
    ReferenceWorkflowHarness,
)


@dataclass(slots=True)
class _ModelSession:
    intent: str
    harness: ReferenceWorkflowHarness
    paused: PausedReferenceRun | None = None
    completed: CompletedReferenceRun | None = None
    history: list[FollowupExchange] = field(default_factory=list)


class ReferenceModelSessionProvider:
    """Deterministic reference "model" exposed through the session seam."""

    def __init__(self) -> None:
        self._sessions: dict[str, _ModelSession] = {}
        self._closed: set[str] = set()

    async def open(
        self,
        *,
        run_id: str,
        intent: str,
        resource_refs: tuple[str, ...],
        host_context_ref: str | None,
    ) -> SessionHandle:
        del resource_refs, host_context_ref  # reference vertical uses its fixed dataset
        session_id = f"session:{run_id}"
        if session_id in self._sessions and session_id not in self._closed:
            raise SessionProviderError("SESSION_ALREADY_OPEN", "one session per Run")
        # Clear the closed flag so _require_open works with the new session
        self._closed.discard(session_id)
        self._sessions[session_id] = _ModelSession(
            intent=intent, harness=ReferenceWorkflowHarness()
        )
        return SessionHandle(session_id=session_id, run_id=run_id)

    async def run_task(self, handle: SessionHandle) -> None:
        session = self._require_open(handle)
        paused = await session.harness.run_to_approval()
        completed = await session.harness.approve_and_complete(
            paused,
            actor_id="demo-approver",
            client_action_id=f"demo-approve:{handle.session_id}",
        )
        session.paused = paused
        session.completed = completed

    async def followup(
        self,
        handle: SessionHandle,
        message: str,
        *,
        read_only: bool = True,
    ) -> str:
        del read_only  # follow-up is read-only by construction
        session = self._require_open(handle)
        if session.completed is None or session.paused is None:
            raise SessionProviderError(
                "TASK_NOT_COMPLETE", "run_task must complete before follow-up"
            )
        answer = self._answer(session.paused, session.completed, message)
        session.history.append(FollowupExchange(question=message, answer=answer))
        return answer

    async def close(self, handle: SessionHandle) -> None:
        self._require_open(handle)
        self._closed.add(handle.session_id)

    def task_summary(self, handle: SessionHandle) -> str:
        """Human-readable summary of the completed task (demo helper)."""
        session = self._require_open(handle)
        if session.completed is None:
            raise SessionProviderError("TASK_NOT_COMPLETE", "run_task must complete first")
        completed = session.completed
        return (
            f"Run {completed.run.run_id} -> {completed.run.status}; "
            f"Effect {completed.effect.effect_id} -> {completed.effect.state} "
            f"(remote {completed.effect.remote_operation_id})"
        )

    def _require_open(self, handle: SessionHandle) -> _ModelSession:
        if handle.session_id not in self._sessions:
            raise SessionProviderError("SESSION_NOT_FOUND", "session was not found")
        if handle.session_id in self._closed:
            raise SessionProviderError("SESSION_CLOSED", "session is closed")
        return self._sessions[handle.session_id]

    @staticmethod
    def _answer(
        paused: PausedReferenceRun,
        completed: CompletedReferenceRun,
        message: str,
    ) -> str:
        analysis = paused.analysis
        effect = completed.effect
        case_count = len(analysis.source.cases)
        if any(key in message for key in ("为什么", "依据", "理由", "通过", "拒绝")):
            return (
                f"任务分析了 {case_count} 条合成用例，发现 {analysis.failed_count} 条失败信号，"
                f"因此生成了缺陷创建提案；审批通过后 Effect 已执行成功，"
                f"远程缺陷单：{effect.remote_operation_id}。"
            )
        if any(key in message for key in ("数据", "查了", "读", "来源")):
            return (
                f"系统读取了合成数据集 {analysis.source.resource_ref}（版本 "
                f"{analysis.source.dataset_version}），共 {case_count} 条用例。"
            )
        return (
            f"当前 Run 状态：{completed.run.status}；Effect：{effect.state}，"
            f"远程操作：{effect.remote_operation_id}。"
        )


__all__ = ["ReferenceModelSessionProvider"]
