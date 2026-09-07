"""Turn-text extraction helper (SDD §13.3 A2).

pi-agent-core does not re-emit provider-level text/thinking deltas to
subscribers, so durable ``agent.turn.completed`` text must be read from the
just-completed assistant message in the Agent state. These tests pin the
extraction contract (object blocks, dict blocks, tool-only turns, fallback).
"""
from __future__ import annotations

from types import SimpleNamespace

from pi_agent_core.types import AssistantMessage, TextContent

from enterprise_agent_platform.execution.runtime import _last_assistant_turn_content


def _agent_with(messages: list[object]) -> object:
    return SimpleNamespace(state=SimpleNamespace(messages=messages))


def test_extracts_text_and_thinking_from_object_blocks() -> None:
    message = AssistantMessage(
        api="deepseek",
        provider="deepseek",
        model="deepseek-chat",
        content=[
            TextContent(text="已确认根因：支付服务到账单服务超时。"),
        ],
    )
    thinking, text = _last_assistant_turn_content(_agent_with([message]))
    assert text == "已确认根因：支付服务到账单服务超时。"
    assert thinking == ""


def test_skips_non_assistant_and_takes_most_recent() -> None:
    older = AssistantMessage(
        api="d", provider="d", model="x", content=[TextContent(text="旧轮次")]
    )
    newer = AssistantMessage(
        api="d", provider="d", model="x", content=[TextContent(text="最新结论")]
    )
    _, text = _last_assistant_turn_content(
        _agent_with([SimpleNamespace(), older, newer])
    )
    assert text == "最新结论"


def test_handles_dict_content_blocks() -> None:
    fake_type = type("AssistantMessage", (), {})
    message = fake_type()
    message.content = [
        {"type": "thinking", "thinking": "逐步排查"},
        {"type": "text", "text": "结论文本"},
        {"type": "tool_use", "id": "c1", "name": "remote_read_tool", "input": {}},
    ]
    thinking, text = _last_assistant_turn_content(_agent_with([message]))
    assert thinking == "逐步排查"
    assert text == "结论文本"
