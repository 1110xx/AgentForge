"""Child-side Agent Runtime for the subprocess pipe transport (Phase 1).

One child Python process per Attempt. The child creates a ``pi-agent-core.Agent``
with a ``PipeStream`` stream_fn (model calls proxied through the pipe) and a full
``ToolRegistry`` (native local tools + remote tools via pipe transport).

Flow inside the child::

    bootstrap → grant (identity + lease)
      → restore checkpoint (run intent / resource refs)
      → create Agent + ToolRegistry + PipeStream
      → Agent.prompt(intent)
        └─ event-driven loop: LLM ↔ tool calls (local + remote)
      → commit_final / record_failure
      → exit code 0 (success) / 1 (fail) / 75+ (protocol or lifecycle failure)

IMPORTANT: the child must never write application logs to stdout — the protocol
stream is stdout. All logs go to stderr.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Sequence
from typing import Any

from pi_agent_core.types import (
    AgentContext,
    AssistantMessage,
    AssistantMessageEvent,
    Model,
    SimpleStreamOptions,
    StreamDoneEvent,
    StreamErrorEvent,
    StreamFn,
    StreamResult,
    StreamStartEvent,
    StreamTextDeltaEvent,
    StreamTextEndEvent,
    StreamTextStartEvent,
    StreamThinkingDeltaEvent,
    StreamThinkingEndEvent,
    StreamThinkingStartEvent,
    StreamToolCallDeltaEvent,
    StreamToolCallEndEvent,
    StreamToolCallStartEvent,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
    UsageCost,
)

from enterprise_agent_platform.execution.pipe_transport import (
    OP_BOOTSTRAP,
    OP_COMMIT_CHECKPOINT,
    OP_COMMIT_FINAL,
    OP_EMIT_EVENT,
    OP_HEARTBEAT,
    OP_MODEL_CALL,
    OP_RECORD_FAILURE,
    OP_RESTORE,
    OP_STREAM_CHUNK,
    PipeClient,
    PipeError,
)
from enterprise_agent_platform.execution.runtime import (
    _coerce_stop_reason,
    AgentRuntime,
    BootstrapGrant,
    RuntimeCheckpoint,
    RuntimeContext,
)
from enterprise_agent_platform.tools.native import create_native_tools
from enterprise_agent_platform.tools.remote import create_remote_tools

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipe-based implementations of legacy protocol types
# ---------------------------------------------------------------------------


class PipeAgentEventSink:
    """Transport implementation of the live-streaming bridge (SDD §11.4/11.5).

    Durable bridge events (tool.execution.started/ended, agent.turn.completed)
    are sent as regular request/response ops so the parent can append them to
    the durable event log and reply; ephemeral stream chunks are fire-and-forget
    (``send_notify``, id=0, no reply) so the Agent loop never blocks on them.
    """

    def __init__(self, client: PipeClient) -> None:
        self._client = client

    async def emit_event(
        self, *, event_type: str, payload: dict[str, object]
    ) -> None:
        await self._client.request(
            OP_EMIT_EVENT, event_type=event_type, payload=payload
        )

    async def stream_chunk(self, *, chunk: dict[str, object]) -> None:
        await self._client.send_notify(OP_STREAM_CHUNK, chunk=chunk)


def _context_dict(context: RuntimeContext) -> dict[str, object]:
    return {
        "attempt_id": context.attempt_id,
        "generation": context.generation,
        "pod_uid": context.pod_uid,
        "runtime_token": context.runtime_token,
        "lease_owner": context.lease_owner,
        "lease_version": context.lease_version,
    }


def _restore_context(raw: dict[str, Any]) -> RuntimeContext:
    return RuntimeContext(
        attempt_id=str(raw["attempt_id"]),
        generation=int(raw["generation"]),
        pod_uid=str(raw.get("pod_uid", "subprocess-local")),
        runtime_token=str(raw.get("runtime_token", "")),
        lease_owner=str(raw.get("lease_owner", "")),
        lease_version=int(raw.get("lease_version", 0)),
    )


class PipeBootstrapClient:
    def __init__(self, client: PipeClient) -> None:
        self._client = client

    async def claim(
        self,
        *,
        bootstrap_token: str,
        pod_uid: str,
        attempt_id: str,
        generation: int,
    ) -> BootstrapGrant:
        result = await self._client.request(
            OP_BOOTSTRAP,
            bootstrap_token=bootstrap_token,
            pod_uid=pod_uid,
            attempt_id=attempt_id,
            generation=generation,
        )
        return BootstrapGrant(
            runtime_token=str(result["runtime_token"]),
            lease_owner=str(result["lease_owner"]),
            lease_version=int(result["lease_version"]),
            expires_at=str(result["expires_at"]),
        )


class PipeRuntimeControlClient:
    def __init__(self, client: PipeClient) -> None:
        self._client = client

    async def restore(self, context: RuntimeContext) -> RuntimeCheckpoint:
        result = await self._client.request(OP_RESTORE, context=_context_dict(context))
        return RuntimeCheckpoint(
            checkpoint_id=str(result["checkpoint_id"]),
            checkpoint_state=str(result["checkpoint_state"]),
            snapshot_state=result.get("snapshot_state"),
            workflow_cursor=dict(result.get("workflow_cursor") or {}),
            agent_state=dict(result.get("agent_state") or {}),
            agent_state_schema_version=result.get("agent_state_schema_version"),
        )

    async def heartbeat(self, context: RuntimeContext) -> RuntimeContext:
        result = await self._client.request(OP_HEARTBEAT, context=_context_dict(context))
        return _restore_context(result)

    async def commit_checkpoint(
        self,
        context: RuntimeContext,
        *,
        agent_state: dict[str, object],
        agent_state_schema_version: str,
    ) -> None:
        """Commit a mid-run checkpoint carrying the Agent snapshot."""
        await self._client.request(
            OP_COMMIT_CHECKPOINT,
            context=_context_dict(context),
            agent_state=agent_state,
            agent_state_schema_version=agent_state_schema_version,
        )

    async def commit_final_checkpoint(
        self,
        context: RuntimeContext,
        *,
        summary: str,
        agent_state: dict[str, object] | None = None,
        agent_state_schema_version: str | None = None,
    ) -> None:
        await self._client.request(
            OP_COMMIT_FINAL,
            context=_context_dict(context),
            summary=summary,
            agent_state=agent_state or {},
            agent_state_schema_version=agent_state_schema_version or "pi-agent-core/v1",
        )

    async def record_failure(
        self, context: RuntimeContext, *, reason_code: str, retryable: bool
    ) -> None:
        await self._client.request(
            OP_RECORD_FAILURE,
            context=_context_dict(context),
            reason_code=reason_code,
            retryable=retryable,
        )


class _EnvIdentityProvider:
    """Reads bootstrap identity from environment instead of Pod volume files."""

    async def provide(self) -> tuple[str, str]:
        token = os.environ.get("AGENT_PLATFORM_BOOTSTRAP_TOKEN", "")
        pod_uid = os.environ.get("AGENT_PLATFORM_POD_UID", "subprocess-local")
        return token, pod_uid


# ---------------------------------------------------------------------------
# PipeStream — stream_fn for pi-agent-core Agent via PipeTransport
# ---------------------------------------------------------------------------


def _make_pipe_stream_fn(client: PipeClient) -> StreamFn:
    """Create a ``StreamFn`` that proxies LLM calls through the pipe.

    The function sends the full message history + tool definitions to the
    parent process, which calls the actual LLM (DeepSeek/Anthropic) and
    returns the response. The response is wrapped into an
    ``AsyncIterator[AssistantMessageEvent]`` stream.
    """

    async def pipe_stream_fn(
        model: Model,
        context: AgentContext,
        options: SimpleStreamOptions,
    ) -> StreamResult:
        queue: asyncio.Queue[AssistantMessageEvent | None] = asyncio.Queue()
        done = asyncio.Event()
        state: dict[str, Any] = {"final": None}

        partial = AssistantMessage(
            api=model.api,
            provider=model.provider,
            model=model.id,
        )

        async def events_iter() -> AsyncIterator[AssistantMessageEvent]:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item

        async def get_result() -> AssistantMessage:
            await done.wait()
            final = state["final"]
            if final is None:
                raise RuntimeError("No result available")
            return final

        async def _run() -> None:
            nonlocal partial
            try:
                # Serialize messages and tools for pipe transport
                serialized_messages = []
                for m in context.messages:
                    if hasattr(m, "model_dump"):
                        serialized_messages.append(m.model_dump())
                    else:
                        serialized_messages.append(m)

                serialized_tools = []
                for t in context.tools:
                    if hasattr(t, "parameters") and hasattr(t.parameters, "model_dump"):
                        params = t.parameters.model_dump()
                    else:
                        params = {"type": "object", "properties": {}, "required": []}
                    serialized_tools.append({
                        "name": t.name,
                        "description": getattr(t, "description", ""),
                        "label": getattr(t, "label", ""),
                        "parameters": params,
                    })

                # Send model_call through pipe
                response = await client.request(
                    OP_MODEL_CALL,
                    model=model.model_dump(),
                    system_prompt=context.system_prompt,
                    messages=serialized_messages,
                    tools=serialized_tools,
                    options={
                        "temperature": options.temperature,
                        "max_tokens": options.max_tokens,
                        "reasoning": options.reasoning,
                    },
                )

                # Parse the response into events
                _emit_response_events(response, partial, queue)

                state["final"] = partial

            except PipeError as error:
                reason = "error"
                partial.stop_reason = reason
                partial.error_message = str(error)
                queue.put_nowait(
                    StreamErrorEvent(reason=reason, error=partial)
                )
                state["final"] = partial

            except Exception as exc:
                reason = "error"
                partial.stop_reason = reason
                partial.error_message = str(exc)
                queue.put_nowait(
                    StreamErrorEvent(reason=reason, error=partial)
                )
                state["final"] = partial

            finally:
                done.set()
                queue.put_nowait(None)

        asyncio.create_task(_run())
        return {"events": events_iter(), "result": get_result}

    return pipe_stream_fn


def _emit_response_events(
    response: dict[str, Any],
    partial: AssistantMessage,
    queue: asyncio.Queue[AssistantMessageEvent | None],
) -> None:
    """Emit events from a non-streaming model response dict.

    Expected response format::

        {
            "content": [
                {"type": "text", "text": "..."},
                {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
            ],
            "stop_reason": "end_turn" | "tool_use" | "max_tokens" | "error",
            "usage": {"input": ..., "output": ..., ...}
        }
    """
    # StreamStart
    queue.put_nowait(StreamStartEvent(partial=partial))

    content_blocks = response.get("content", [])
    for idx, block in enumerate(content_blocks):
        block_type = block.get("type", "text")

        if block_type == "text":
            text = block.get("text", "")
            # TextStart
            partial.content.append(TextContent())
            queue.put_nowait(StreamTextStartEvent(content_index=idx, partial=partial))
            # TextDelta (full text as single delta for non-streaming)
            partial.content[idx] = TextContent(text=text)
            queue.put_nowait(StreamTextDeltaEvent(
                content_index=idx, delta=text, partial=partial
            ))
            # TextEnd
            queue.put_nowait(StreamTextEndEvent(
                content_index=idx, content=text, partial=partial
            ))

        elif block_type == "thinking":
            thinking = block.get("thinking", "")
            signature = block.get("signature")
            # ThinkingStart
            partial.content.append(ThinkingContent())
            queue.put_nowait(StreamThinkingStartEvent(content_index=idx, partial=partial))
            # ThinkingDelta
            content_obj = ThinkingContent(thinking=thinking, thinking_signature=signature)
            partial.content[idx] = content_obj
            queue.put_nowait(StreamThinkingDeltaEvent(
                content_index=idx, delta=thinking, partial=partial
            ))
            # ThinkingEnd
            partial.content[idx] = ThinkingContent(thinking=thinking, thinking_signature=signature)
            queue.put_nowait(StreamThinkingEndEvent(
                content_index=idx, content=thinking, partial=partial
            ))

        elif block_type == "tool_use" or block_type == "tool_call":
            tool_id = block.get("id", "")
            tool_name = block.get("name", "")
            tool_input = block.get("input", {})
            # ToolCallStart
            tc = ToolCall(id=tool_id, name=tool_name)
            partial.content.append(tc)
            queue.put_nowait(StreamToolCallStartEvent(content_index=idx, partial=partial))
            # ToolCallDelta
            partial_json = json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
            tc.partial_json = partial_json
            tc.arguments = tool_input
            queue.put_nowait(StreamToolCallDeltaEvent(
                content_index=idx, delta=partial_json, partial=partial
            ))
            # ToolCallEnd
            tc.partial_json = None
            queue.put_nowait(StreamToolCallEndEvent(
                content_index=idx, tool_call=tc, partial=partial
            ))

    # Done
    # The PipeStream parent answers with the pi-agent enum
    # ("end_turn"/"tool_use"/"max_tokens"); the Internal API StreamDoneEvent
    # contract uses the proxy-style values ("stop"/"toolUse"/"length"), so
    # coerce before emitting — same mapping runtime._coerce_stop_reason uses
    # for restore hydration.
    stop_reason = _coerce_stop_reason(response.get("stop_reason", "stop"))
    partial.stop_reason = stop_reason
    usage_data = response.get("usage", {})
    cost_data = usage_data.get("cost", {})
    partial.usage = Usage(
        input=usage_data.get("input", 0),
        output=usage_data.get("output", 0),
        cache_read=usage_data.get("cacheRead", 0) or usage_data.get("cache_read", 0),
        cache_write=usage_data.get("cacheWrite", 0) or usage_data.get("cache_write", 0),
        total_tokens=usage_data.get("totalTokens", 0) or usage_data.get("total_tokens", 0),
        cost=UsageCost(
            input=cost_data.get("input", 0),
            output=cost_data.get("output", 0),
            cache_read=cost_data.get("cacheRead", 0) or cost_data.get("cache_read", 0),
            cache_write=cost_data.get("cacheWrite", 0) or cost_data.get("cache_write", 0),
            total=cost_data.get("total", 0),
        ),
    )
    queue.put_nowait(StreamDoneEvent(reason=stop_reason, message=partial))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _main() -> int:
    from enterprise_agent_platform.platform.logging_json import (
        install_json_logs_if_enabled,
    )

    install_json_logs_if_enabled()
    attempt_id = os.environ.get("AGENT_PLATFORM_ATTEMPT_ID", "")
    generation_raw = os.environ.get("AGENT_PLATFORM_GENERATION", "1")
    if not attempt_id:
        logger.error("AGENT_PLATFORM_ATTEMPT_ID is required")
        return 77
    generation = int(generation_raw) if generation_raw.isdigit() else 1

    client = PipeClient()
    await client.start()
    try:
        bootstrap = PipeBootstrapClient(client)
        control = PipeRuntimeControlClient(client)

        # Build AgentRuntime with pi-agent-core integration
        runtime = AgentRuntime(
            bootstrap,
            control,
            identity_provider=_EnvIdentityProvider(),
        )

        # Live-streaming bridge (SDD §11.4/§11.5): durable bridge events via
        # OP_EMIT_EVENT (parent appends an EnterpriseEventEnvelope), ephemeral
        # deltas via OP_STREAM_CHUNK (parent forwards to the in-memory relay
        # only — never persisted). Both are fire-safe on the pipe.
        runtime.set_event_sink(PipeAgentEventSink(client))

        # Set up tools
        runtime.set_tools(
            native_tools=create_native_tools(),
            remote_tools=create_remote_tools(client),
        )

        # Set up PipeStream stream_fn for LLM calls
        runtime.set_stream_fn(_make_pipe_stream_fn(client))

        # Run the agent
        return await runtime.run(
            attempt_id=attempt_id,
            generation=generation,
        )
    except PipeError as error:
        logger.error("pipe protocol error: %s", error)
        return 76
    finally:
        await client.close()


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] (child) %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())