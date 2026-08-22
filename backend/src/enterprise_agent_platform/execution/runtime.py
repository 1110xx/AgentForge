"""Explicit one-Attempt Agent Runtime loop: lifecycle shell + pi-agent-core Agent.

``AgentRuntime`` is now a **lifecycle shell**: it handles bootstrap identity,
checkpoint restore, heartbeat keep-alive, and final checkpoint commit/record.
The actual agent decision loop is delegated to ``pi-agent-core.Agent`` via
``Agent.prompt()`` / ``Agent.continue_()`` with an event-driven ToolRegistry.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pi_agent_core.agent import Agent, AgentOptions
from pi_agent_core.types import (
    AgentEvent,
    AgentTool,
    AssistantMessage,
    Message,
    Model,
    StreamFn,
    ToolResultMessage,
    TurnEndEvent,
    UserMessage,
)

from enterprise_agent_platform.persistence.protocol import PlatformError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legacy types (kept for backwards compatibility during transition)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BootstrapGrant:
    runtime_token: str
    lease_owner: str
    lease_version: int
    expires_at: str
    # Subject facts the Pod needs to sign HTTP runtime ops (empty for the
    # pipe transport, which carries them on the parent ticket instead).
    tenant_id: str = ""
    run_id: str = ""
    execution_unit_id: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    attempt_id: str
    generation: int
    pod_uid: str
    runtime_token: str
    lease_owner: str
    lease_version: int
    tenant_id: str = ""
    run_id: str = ""
    execution_unit_id: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeCheckpoint:
    checkpoint_id: str
    checkpoint_state: str
    snapshot_state: str | None
    workflow_cursor: dict[str, object]
    agent_state: dict[str, object] | None = None
    agent_state_schema_version: str | None = None


class BootstrapClient(Protocol):
    async def claim(
        self,
        *,
        bootstrap_token: str,
        pod_uid: str,
        attempt_id: str,
        generation: int,
    ) -> BootstrapGrant: ...


class RuntimeControlClient(Protocol):
    async def restore(self, context: RuntimeContext) -> RuntimeCheckpoint: ...
    async def heartbeat(self, context: RuntimeContext) -> RuntimeContext: ...
    async def commit_checkpoint(
        self,
        context: RuntimeContext,
        *,
        agent_state: dict[str, object],
        agent_state_schema_version: str,
    ) -> None: ...
    async def commit_final_checkpoint(
        self,
        context: RuntimeContext,
        *,
        summary: str,
        agent_state: dict[str, object] | None = None,
        agent_state_schema_version: str | None = None,
    ) -> None: ...
    async def record_failure(
        self, context: RuntimeContext, *, reason_code: str, retryable: bool
    ) -> None: ...


class RuntimeIdentityProvider(Protocol):
    """Alternative bootstrap identity source for non-Pod runtimes."""

    async def provide(self) -> tuple[str, str]:
        """Return ``(bootstrap_token, pod_uid)``."""
        ...


# ---------------------------------------------------------------------------
# Agent state rehydration (pi-agent-core snapshot -> AgentOptions.initial_state)
# ---------------------------------------------------------------------------


def _restore_agent_state(
    snapshot: dict[str, object],
    default_system_prompt: str,
    tools: list[AgentTool],
) -> dict[str, object]:
    """Rebuild the mutable part of ``AgentOptions.initial_state`` from a snapshot.

    ``tools`` are intentionally not restored from the snapshot (their
    ``execute`` callables are excluded from serialization); the caller passes
    the locally registered tool list and the value is used only to seed the
    restored ``messages`` when no system_prompt is present in the snapshot.
    """
    del tools
    state: dict[str, object] = {}

    snapshot_system_prompt = snapshot.get("system_prompt")
    state["system_prompt"] = (
        str(snapshot_system_prompt) if snapshot_system_prompt else default_system_prompt
    )

    thinking = snapshot.get("thinking_level")
    if thinking:
        state["thinking_level"] = thinking

    model_data = snapshot.get("model")
    if isinstance(model_data, dict) and model_data.get("id"):
        try:
            state["model"] = Model.model_validate(model_data)
        except Exception:
            pass

    messages: list[Message] = []
    for raw in snapshot.get("messages") or []:
        if not isinstance(raw, dict):
            continue
        try:
            role = raw.get("role")
            if role == "user":
                messages.append(UserMessage.model_validate(_coerce_message(raw)))
            elif role == "assistant":
                messages.append(AssistantMessage.model_validate(_coerce_message(raw)))
            elif role == "toolResult":
                messages.append(ToolResultMessage.model_validate(raw))
        except Exception:
            continue
    if messages:
        state["messages"] = messages

    return state


# Proxy-terminology normalisation: the PipeStream parent answers LLM calls with
# ``end_turn`` / ``tool_use`` / ``max_tokens`` stop reasons, and the child stores
# them via direct attribute assignment (bypassing Pydantic literal validation).
# On restore they must be coerced back to the pi-agent-core Python enum values
# (``stop`` / ``toolUse`` / ``length``) or ``model_validate`` would drop the
# whole message and silently lose conversation history.


def _coerce_stop_reason(value: object) -> object:
    if not isinstance(value, str):
        return value
    return {
        "end_turn": "stop",
        "tool_use": "toolUse",
        "max_tokens": "length",
    }.get(value, value)


def _coerce_content_block(block: object) -> object:
    if not isinstance(block, dict):
        return block
    block_type = block.get("type")
    if block_type in ("tool_use", "tool_call"):
        return {
            "type": "toolCall",
            "id": block.get("id", ""),
            "name": block.get("name", ""),
            "arguments": block.get("input", block.get("arguments", {})),
            "partial_json": block.get("partial_json"),
        }
    if block_type == "thinking":
        # Proxy emits ``signature``; the Python model expects ``thinking_signature``.
        if "signature" in block and "thinking_signature" not in block:
            block = dict(block)
            block["thinking_signature"] = block.pop("signature")
    return block


def _coerce_message(raw: dict[str, object]) -> dict[str, object]:
    """Normalise a serialized message before Pydantic reconstruction."""
    role = raw.get("role")
    if role != "assistant":
        return raw
    result = dict(raw)
    stop_reason = raw.get("stop_reason")
    if stop_reason is not None:
        result["stop_reason"] = _coerce_stop_reason(stop_reason)
    content = raw.get("content")
    if isinstance(content, list):
        result["content"] = [_coerce_content_block(block) for block in content]
    return result


# ---------------------------------------------------------------------------
# AgentRuntime — lifecycle shell
# ---------------------------------------------------------------------------


class AgentRuntime:
    """Lifecycle shell around pi-agent-core Agent.

    Responsibilities:
    - Bootstrap identity (env/volume files or identity provider)
    - Claim lease via BootstrapClient
    - Restore checkpoint via RuntimeControlClient
    - Assemble ToolRegistry (native + remote) and create pi-agent-core Agent
    - Run Agent.prompt() with the run intent, wait for AgentEndEvent
    - Heartbeat keep-alive (on each TurnEndEvent)
    - Commit final checkpoint or record failure on exit
    - Map exit codes (0=SUCCESS, 1=FAIL, 75+=SYSTEM)
    """

    def __init__(
        self,
        bootstrap: BootstrapClient,
        control: RuntimeControlClient,
        bootstrap_token_path: Path = Path("/runtime/bootstrap/bootstrap-token"),
        pod_uid_path: Path = Path("/runtime/pod/pod-uid"),
        identity_provider: RuntimeIdentityProvider | None = None,
    ) -> None:
        self._bootstrap = bootstrap
        self._control = control
        self._bootstrap_token_path = bootstrap_token_path
        self._pod_uid_path = pod_uid_path
        self._identity_provider = identity_provider

        # Injected after construction (set by subprocess_runtime / local_runtime)
        self._native_tools: list[AgentTool] = []
        self._remote_tools: list[AgentTool] = []
        self._stream_fn: StreamFn | None = None
        self._get_api_key: Any = None
        # Latest refreshed RuntimeContext (lease_version kept fresh across
        # heartbeats so turn-level checkpoint CAS does not go stale).
        self._context: RuntimeContext | None = None
        # In-flight turn-level tasks (heartbeat refresh + checkpoint commit)
        # that must complete before the final commit to avoid CAS races.
        self._pending_tasks: list[asyncio.Task[None]] = []

    def set_tools(
        self, native_tools: list[AgentTool], remote_tools: list[AgentTool]
    ) -> None:
        self._native_tools = native_tools
        self._remote_tools = remote_tools

    def set_stream_fn(self, stream_fn: StreamFn) -> None:
        self._stream_fn = stream_fn

    def set_get_api_key(self, get_api_key: Any) -> None:
        self._get_api_key = get_api_key

    async def run(
        self,
        *,
        attempt_id: str,
        generation: int,
        model: Model | None = None,
        tools: list[AgentTool] | None = None,
    ) -> int:
        # ── Step 1: Bootstrap identity ──
        try:
            if self._identity_provider is not None:
                bootstrap_token, pod_uid = await self._identity_provider.provide()
            else:
                bootstrap_token, pod_uid = await asyncio.gather(
                    asyncio.to_thread(self._bootstrap_token_path.read_text),
                    asyncio.to_thread(self._pod_uid_path.read_text),
                )
            bootstrap_token = bootstrap_token.strip()
            pod_uid = pod_uid.strip()
            if not bootstrap_token or not pod_uid:
                return 77
        except (OSError, TimeoutError):
            return 77

        # ── Step 2: Claim lease ──
        try:
            grant = await self._bootstrap.claim(
                bootstrap_token=bootstrap_token,
                pod_uid=pod_uid,
                attempt_id=attempt_id,
                generation=generation,
            )
        except PlatformError:
            return 77

        context = RuntimeContext(
            attempt_id=attempt_id,
            generation=generation,
            pod_uid=pod_uid,
            runtime_token=grant.runtime_token,
            lease_owner=grant.lease_owner,
            lease_version=grant.lease_version,
            # HTTP ops (restore/heartbeat/checkpoints/model-call) are signed
            # with the subject facts from the bootstrap grant — without them
            # the Pod sends empty tenant_id/run_id and every Internal API op
            # fails validation (L3 gate catch).
            tenant_id=grant.tenant_id,
            run_id=grant.run_id,
            execution_unit_id=grant.execution_unit_id,
        )
        self._context = context

        # ── Step 3: Restore checkpoint ──
        try:
            checkpoint = await self._control.restore(context)
            if checkpoint.checkpoint_state != "COMMITTED" or checkpoint.snapshot_state not in (
                None,
                "READY",
            ):
                return 78
        except PlatformError:
            return 78

        # ── Step 4: Assemble Agent ──
        intent = str(checkpoint.workflow_cursor.get("intent", ""))
        resource_refs = list(checkpoint.workflow_cursor.get("resource_refs", ()))
        # Follow-up reactivation: the fresh Attempt restores the Run cursor with
        # a followup_question, so the Agent answers the question instead of
        # re-running the original intent (SDD §6.4).
        followup_question = checkpoint.workflow_cursor.get("followup_question")
        system_prompt = self._build_system_prompt(intent, resource_refs)

        all_tools = (tools or []) + self._native_tools + self._remote_tools

        # Checkpoint rehydration: if the committed Checkpoint carries a
        # pi-agent-core Agent snapshot (``Agent.state.model_dump()``), rebuild
        # the instance memory (system prompt / model / thinking level /
        # messages). Tools are deliberately NOT taken from the snapshot — the
        # snapshot has no ``execute`` callables — they are always re-registered
        # from the local ToolRegistry so restored history can call them again.
        agent_state = dict(checkpoint.agent_state or {})
        initial_state = _restore_agent_state(
            agent_state, system_prompt, all_tools
        )
        opts = AgentOptions(
            convert_to_llm=self._convert_to_llm,
            stream_fn=self._stream_fn,
            get_api_key=self._get_api_key,
            initial_state=initial_state,
        )
        agent = Agent(opts)
        if model:
            agent.set_model(model)
        elif not bool(initial_state.get("model")):
            # Fresh run without a restored model: the parent proxies LLM calls
            # via OP_MODEL_CALL regardless, but ``Agent.prompt()`` gates on a
            # non-empty ``model.id`` — provide the platform default.
            agent.set_model(
                Model(api="deepseek", provider="deepseek", id="deepseek-chat")
            )
        agent.set_tools(all_tools)

        # Follow-up vs fresh intent: with restored conversation history the
        # original intent is already in ``messages``, so only the question is
        # prompted; without history keep the previous ``intent + question`` text.
        has_history = bool(initial_state.get("messages"))
        user_prompt = intent
        if followup_question:
            user_prompt = (
                f"[Follow-up question]\n{followup_question}"
                if has_history
                else f"{intent}\n\n[Follow-up question]\n{followup_question}"
            )

        # ── Step 5: Subscribe to events for heartbeat + lifecycle ──
        agent.subscribe(lambda e: self._on_agent_event(e, context, agent))

        # ── Step 6: Run Agent.prompt() ──
        summary: str | None = None
        exit_code: int = 79  # default: max decisions reached

        try:
            await agent.prompt(user_prompt)

            # After AgentEndEvent, check the final message
            if agent.state.error:
                log.warning("agent reported error: %s", agent.state.error)
                exit_code = 1
            else:
                # Success path — extract summary from last assistant message
                last_msgs = agent.state.messages
                for msg in reversed(last_msgs):
                    if isinstance(msg, AssistantMessage):
                        summary = self._extract_text(msg)
                        break
                exit_code = 0

        except Exception as exc:
            log.error("agent loop failed: %s", exc, exc_info=True)
            exit_code = 1

        # ── Step 7: Commit or fail ──
        try:
            if exit_code == 0 and summary:
                # Drain any in-flight turn checkpoints so the final CAS cannot
                # race a concurrent CHECKPOINTING transition.
                await self._drain_pending_tasks()
                # Refresh the lease so the final checkpoint carries a fresh
                # lease_version (turn-level commits already wrote Agent
                # snapshots; this terminal write makes the final state durable
                # before the Run transitions to SUCCEEDED). Use the freshest
                # context (turn-level heartbeats bump the lease_version, so
                # the original Step-2 context would CAS-fail with a 409).
                try:
                    context = await self._control.heartbeat(self._context)
                    self._context = context
                except Exception:
                    context = self._context
                await self._control.commit_final_checkpoint(
                    context,
                    summary=summary,
                    agent_state=_export_agent_state(agent),
                    agent_state_schema_version=_AGENT_STATE_SCHEMA_VERSION,
                )
                return 0
            else:
                await self._control.record_failure(
                    context,
                    reason_code="AGENT_FAILURE" if exit_code == 1 else "SYSTEM_ERROR",
                    retryable=(exit_code < 75),
                )
                return exit_code
        except PlatformError as error:
            if error.code in (
                "STALE_GENERATION",
                "LEASE_EXPIRED",
                "LEASE_OWNER_MISMATCH",
                "VERSION_CONFLICT",
            ):
                return 75
            raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_system_prompt(self, intent: str, resource_refs: list[str]) -> str:
        parts = [f"## Task Intent\n\n{intent}"]
        if resource_refs:
            parts.append(
                "\n\n## Available Resources\n\n"
                + "\n".join(f"- `{ref}`" for ref in resource_refs)
            )
        parts.append(
            "\n\nYou have access to local workspace tools (file_read, file_write, bash) "
            "and remote platform tools (remote_read_tool, remote_publish_artifact, "
            "remote_propose_action). Use remote_read_tool to fetch the resources listed "
            "above for analysis."
        )
        return "\n".join(parts)

    def _on_agent_event(
        self,
        event: AgentEvent,
        context: RuntimeContext,
        agent: Agent,
    ) -> None:
        """Event callback: heartbeat + turn-level checkpoint on TurnEndEvent.

        ``TurnEndEvent`` is emitted by the pi-agent-core loop only after every
        tool call of the turn has completed (never mid-execution), so the
        snapshot taken here is a safe checkpoint boundary (proposal 坑1).
        ``model_dump()`` runs synchronously inside the callback while the loop
        is suspended at the yield point, so it cannot race the next turn's
        state mutation.
        """
        if isinstance(event, TurnEndEvent):
            try:
                loop = asyncio.get_running_loop()
                self._pending_tasks.append(
                    loop.create_task(self._heartbeat_and_refresh(context))
                )
                self._pending_tasks.append(
                    loop.create_task(self._commit_turn_checkpoint(context, agent))
                )
            except RuntimeError:
                pass

    async def _drain_pending_tasks(self) -> None:
        """Await outstanding heartbeat / turn-checkpoint tasks before the final commit."""
        pending = [task for task in self._pending_tasks if not task.done()]
        self._pending_tasks.clear()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _heartbeat_and_refresh(self, context: RuntimeContext) -> None:
        """Refresh the Lease and keep the shared context's lease_version fresh."""
        try:
            refreshed = await self._control.heartbeat(context)
            self._context = refreshed
        except Exception as exc:
            log.warning("heartbeat refresh failed (non-fatal): %s", exc)

    async def _commit_turn_checkpoint(
        self, context: RuntimeContext, agent: Agent
    ) -> None:
        """Best-effort mid-run checkpoint carrying the Agent snapshot.

        A failed turn commit (e.g. Lease version CAS race) is non-fatal: the
        final checkpoint on AgentEnd still persists the last state.
        """
        try:
            agent_state = _export_agent_state(agent)
            await self._control.commit_checkpoint(
                self._context or context,
                agent_state=agent_state,
                agent_state_schema_version=_AGENT_STATE_SCHEMA_VERSION,
            )
        except Exception as exc:
            log.warning("turn checkpoint commit failed (non-fatal): %s", exc)

    @staticmethod
    def _extract_text(msg: AssistantMessage) -> str:
        from pi_agent_core.types import TextContent

        texts = [c.text for c in msg.content if isinstance(c, TextContent)]
        return "\n".join(texts) if texts else msg.model_dump_json()

    @staticmethod
    def _convert_to_llm(messages: list[Message]) -> list[Message]:
        """Keep only LLM-compatible messages (user, assistant, toolResult)."""
        from pi_agent_core.types import AssistantMessage, ToolResultMessage, UserMessage

        return [
            m
            for m in messages
            if isinstance(m, (UserMessage, AssistantMessage, ToolResultMessage))
        ]


# ---------------------------------------------------------------------------
# Agent state export (pi-agent-core in-memory state -> durable snapshot)
# ---------------------------------------------------------------------------

_AGENT_STATE_SCHEMA_VERSION = "pi-agent-core/v1"


def _export_agent_state(agent: Agent) -> dict[str, object]:
    """Serialize the Agent's stable state for durable Checkpoint storage.

    Only LLM-relevant, JSON-safe fields are persisted: system prompt, model,
    thinking level, tool schemas (``execute`` callables are excluded by the
    core model) and the full message history. Transient streaming fields
    (``is_streaming`` / ``stream_message`` / ``pending_tool_calls`` / ``error``)
    are dropped — at TurnEnd/AgentEnd they carry no recoverable value.
    """
    dumped = agent.state.model_dump()
    return {
        "system_prompt": dumped.get("system_prompt", ""),
        "model": dumped.get("model"),
        "thinking_level": dumped.get("thinking_level"),
        "tools": dumped.get("tools", []),
        "messages": dumped.get("messages", []),
    }


# ---------------------------------------------------------------------------
# Module entry — what the K8s Job Pod actually runs
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m enterprise_agent_platform.execution.runtime``.

    The K8s Job (``job_spec.build_attempt_job``) spawns this module inside the
    Pod. The Pod is an HTTP Runner: it drives the Control-Plane Internal API
    (bootstrap → restore → heartbeat → model-call → checkpoints) via
    ``execution.http_runtime``. Subprocess mode runs a different module
    (``execution.subprocess_runtime``) over the pipe; running this one without
    a control-plane URL fails closed with exit 77.
    """
    del argv
    if not os.environ.get("AGENT_PLATFORM_CONTROL_PLANE_URL"):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        log.error(
            "AGENT_PLATFORM_CONTROL_PLANE_URL is required when running "
            "execution.runtime directly (HTTP Pod mode); subprocess mode uses "
            "execution.subprocess_runtime"
        )
        return 77
    from enterprise_agent_platform.execution.http_runtime import main as http_main

    return http_main()


if __name__ == "__main__":
    raise SystemExit(main())