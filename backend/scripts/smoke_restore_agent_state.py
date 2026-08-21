"""Smoke test: pi-agent-core snapshot export -> Runner-side rehydration.

Proves the restore path: a real ``pi_agent_core.Agent`` state snapshot
(``agent.state.model_dump()``) round-trips through the Runner adapter helper
(``runtime._restore_agent_state``) back into an ``Agent`` whose memory
(system_prompt / model / thinking_level / messages) is intact and whose tools
are re-registered from the local registry. Proxy-path terminology
(``end_turn`` / ``tool_use``) is normalized back to the Python enum values.
"""
from __future__ import annotations

from pi_agent_core import (
    Agent,
    AgentOptions,
    AgentTool,
    AgentToolResult,
    AgentToolSchema,
    Model,
    TextContent,
)
from pi_agent_core.types import AssistantMessage, ToolCall, UserMessage


async def _read_tool(tool_call_id, params, cancel_event=None, on_update=None):  # type: ignore[no-untyped-def]
    del tool_call_id, params, cancel_event, on_update
    return AgentToolResult(content=[TextContent(text="file content")])


def _make_tool() -> AgentTool:
    return AgentTool(
        name="file_read",
        description="read a workspace file",
        parameters=AgentToolSchema(
            properties={"path": {"type": "string"}},
            required=["path"],
        ),
        execute=_read_tool,
    )


def _build_source_agent() -> Agent:
    tool_call = ToolCall(id="call-1", name="file_read", arguments={"path": "/tmp/a.txt"})
    agent = Agent(AgentOptions())
    agent.set_system_prompt("You are the checkpoint test agent.")
    agent.set_model(Model(api="deepseek", provider="deepseek", id="deepseek-chat"))
    agent.set_thinking_level("low")
    agent.set_tools([_make_tool()])
    agent.state.messages = [
        UserMessage(content=[TextContent(text="First question")]),
        # Assistant message that called a tool (proxy terminology variant).
        AssistantMessage(content=[tool_call], stop_reason="toolUse"),
        UserMessage(content=[TextContent(text="Second question")]),
    ]
    return agent


def _main() -> None:
    from enterprise_agent_platform.execution.runtime import _restore_agent_state

    source = _build_source_agent()
    snapshot = source.state.model_dump()
    assert "execute" not in snapshot["tools"][0], "execute callable must be excluded"

    # Simulate the PipeStream parent's serialized terminology, exactly as the
    # child exports it after ``_emit_response_events`` (direct assignment to
    # stop_reason bypasses Pydantic literal validation).
    for message in snapshot["messages"]:
        if message.get("role") == "assistant":
            message["stop_reason"] = "end_turn"
            for block in message.get("content", []):
                if block.get("type") == "toolCall":
                    block["type"] = "tool_use"
                    block["input"] = block.pop("arguments")

    initial_state = _restore_agent_state(
        snapshot,
        default_system_prompt="fallback system prompt",
        tools=[_make_tool()],
    )
    restored = Agent(
        AgentOptions(initial_state=initial_state, stream_fn=lambda *a, **k: None)
    )
    restored.set_tools([_make_tool()])  # re-register local tools like the Runner does

    assert restored.state.system_prompt == "You are the checkpoint test agent."
    assert restored.state.thinking_level == "low"
    assert restored.state.model.id == "deepseek-chat"
    assert restored.state.model.api == "deepseek"
    messages = restored.state.messages
    assert len(messages) == 3
    assert isinstance(messages[0], UserMessage) and messages[0].content[0].text == "First question"
    assert isinstance(messages[1], AssistantMessage)
    assert messages[1].stop_reason == "stop", "end_turn must be normalized to stop"
    assert len(messages[1].content) == 1
    assert isinstance(messages[1].content[0], ToolCall)
    assert messages[1].content[0].name == "file_read"
    assert messages[1].content[0].arguments == {"path": "/tmp/a.txt"}
    assert isinstance(messages[2], UserMessage) and messages[2].content[0].text == "Second question"

    # The prompt gate requires a non-empty model.id: restored state satisfies it.
    assert restored.state.model.id, "Agent.prompt() would raise 'No model configured'"
    assert len(restored.state.tools) == 1 and restored.state.tools[0].name == "file_read"

    print("restore rehydration OK: system_prompt/thinking/model/messages/tools intact")
    print("proxy terminology normalized: stop_reason=end_turn -> stop; tool_use -> ToolCall")


if __name__ == "__main__":
    _main()