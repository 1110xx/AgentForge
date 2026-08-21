"""Local demo: a model provider runs a complete task and answers follow-ups.

Runs the deterministic reference vertical through the RunSessionProvider seam,
showing "one Run = one model provider session": the task executes inside the
session, then follow-up questions continue in the same session.
"""
from __future__ import annotations

import asyncio

from enterprise_agent_platform.reference.model_provider import ReferenceModelSessionProvider


async def main() -> None:
    provider = ReferenceModelSessionProvider()

    handle = await provider.open(
        run_id="demo-run-1",
        intent="分析合成失败数据集并创建缺陷单",
        resource_refs=("synthetic-dataset:reference",),
        host_context_ref=None,
    )
    print(f"[open] session {handle.session_id} bound to run {handle.run_id}")

    await provider.run_task(handle)
    print(f"[task] {provider.task_summary(handle)}")

    for question in (
        "为什么判定需要创建缺陷单？",
        "查了哪些数据？",
        "当前任务结果是什么？",
    ):
        answer = await provider.followup(handle, question)
        print(f"[q] {question}\n[a] {answer}\n")

    await provider.close(handle)
    print("[close] session closed")


if __name__ == "__main__":
    asyncio.run(main())
