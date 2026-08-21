"""Explicit one-Attempt Agent Runtime loop: lifecycle shell + pi-agent-core Agent.

``AgentRuntime`` is now a **lifecycle shell**: it handles bootstrap identity,
checkpoint restore, heartbeat keep-alive, and final checkpoint commit/record.
The actual agent decision loop is delegated to ``pi-agent-core.Agent`` via
``Agent.prompt()`` / ``Agent.continue_()`` with an event-driven ToolRegistry.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pi_agent_core.agent import Agent, AgentOptions
from pi_agent_core.types import (
    AgentContext,
    AgentEndEvent,
    AgentEvent,
    AgentLoopConfig,
    AgentTool,
    AssistantMessage,
    Message,
    Model,
    SimpleStreamOptions,
    StreamFn,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    Usage,
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


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    attempt_id: str
    generation: int
    pod_uid: str
    runtime_token: str
    lease_owner: str
    lease_version: int


@dataclass(frozen=True, slots=True)
class RuntimeCheckpoint:
    checkpoint_id: str
    checkpoint_state: str
    snapshot_state: str | None
    workflow_cursor: dict[str, object]


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
    async def commit_final_checkpoint(
        self, context: RuntimeContext, *, summary: str
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
        )

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
        user_prompt = intent
        if followup_question:
            user_prompt = f"{intent}\n\n[Follow-up question]\n{followup_question}"
        system_prompt = self._build_system_prompt(intent, resource_refs)

        all_tools = (tools or []) + self._native_tools + self._remote_tools

        opts = AgentOptions(
            convert_to_llm=self._convert_to_llm,
            stream_fn=self._stream_fn,
            get_api_key=self._get_api_key,
        )
        agent = Agent(opts)
        agent.set_system_prompt(system_prompt)
        if model:
            agent.set_model(model)
        agent.set_tools(all_tools)

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
                await self._control.commit_final_checkpoint(
                    context, summary=summary
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
        """Event callback: heartbeat on turn end, etc."""
        if isinstance(event, TurnEndEvent):
            try:
                context = asyncio.get_running_loop().create_task(
                    self._control.heartbeat(context)
                )
            except RuntimeError:
                pass

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