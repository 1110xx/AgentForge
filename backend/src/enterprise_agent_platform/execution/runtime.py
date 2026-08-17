"""Explicit one-Attempt Agent Runtime loop with one-shot bootstrap identity."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from enterprise_agent_platform.persistence.protocol import PlatformError


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
class ProviderContext:
    attempt_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class RuntimeCheckpoint:
    checkpoint_id: str
    checkpoint_state: str
    snapshot_state: str | None
    workflow_cursor: dict[str, object]


@dataclass(frozen=True, slots=True)
class ReadToolDecision:
    tool_name: str
    arguments_ref: str
    kind: Literal["read_tool"] = "read_tool"


@dataclass(frozen=True, slots=True)
class PublishArtifactDecision:
    workspace_path: str
    logical_name: str
    classification: str
    kind: Literal["publish_artifact"] = "publish_artifact"


@dataclass(frozen=True, slots=True)
class ProposeActionDecision:
    action_ref: str
    canonical_payload_ref: str
    kind: Literal["propose_action"] = "propose_action"


@dataclass(frozen=True, slots=True)
class CompleteDecision:
    summary: str
    kind: Literal["complete"] = "complete"


@dataclass(frozen=True, slots=True)
class FailDecision:
    reason_code: str
    retryable: bool
    kind: Literal["fail"] = "fail"


type RuntimeDecision = (
    ReadToolDecision
    | PublishArtifactDecision
    | ProposeActionDecision
    | CompleteDecision
    | FailDecision
)


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
    async def read_tool(self, context: RuntimeContext, decision: ReadToolDecision) -> None: ...
    async def publish_artifact(
        self, context: RuntimeContext, decision: PublishArtifactDecision
    ) -> None: ...
    async def propose_action(
        self, context: RuntimeContext, decision: ProposeActionDecision
    ) -> None: ...
    async def commit_final_checkpoint(
        self, context: RuntimeContext, decision: CompleteDecision
    ) -> None: ...
    async def record_failure(self, context: RuntimeContext, decision: FailDecision) -> None: ...


class AgentProvider(Protocol):
    async def decide(
        self, context: ProviderContext, checkpoint: RuntimeCheckpoint
    ) -> RuntimeDecision: ...


class AgentRuntime:
    def __init__(
        self,
        bootstrap: BootstrapClient,
        control: RuntimeControlClient,
        provider: AgentProvider,
        bootstrap_token_path: Path = Path("/runtime/bootstrap/bootstrap-token"),
        pod_uid_path: Path = Path("/runtime/pod/pod-uid"),
        max_decisions: int = 1000,
    ) -> None:
        self._bootstrap = bootstrap
        self._control = control
        self._provider = provider
        self._bootstrap_token_path = bootstrap_token_path
        self._pod_uid_path = pod_uid_path
        self._max_decisions = max_decisions

    async def run(self, *, attempt_id: str, generation: int) -> int:
        try:
            bootstrap_token, pod_uid = await asyncio.gather(
                asyncio.to_thread(self._bootstrap_token_path.read_text),
                asyncio.to_thread(self._pod_uid_path.read_text),
            )
            bootstrap_token = bootstrap_token.strip()
            pod_uid = pod_uid.strip()
            if not bootstrap_token or not pod_uid:
                return 77
            grant = await self._bootstrap.claim(
                bootstrap_token=bootstrap_token,
                pod_uid=pod_uid,
                attempt_id=attempt_id,
                generation=generation,
            )
            bootstrap_token = ""  # projected token is never used after the one claim.
        except (OSError, PlatformError, TimeoutError):
            return 77

        context = RuntimeContext(
            attempt_id=attempt_id,
            generation=generation,
            pod_uid=pod_uid,
            runtime_token=grant.runtime_token,
            lease_owner=grant.lease_owner,
            lease_version=grant.lease_version,
        )
        try:
            checkpoint = await self._control.restore(context)
            if checkpoint.checkpoint_state != "COMMITTED" or checkpoint.snapshot_state not in (
                None,
                "READY",
            ):
                return 78
            for _ in range(self._max_decisions):
                context = await self._control.heartbeat(context)
                decision = await self._provider.decide(
                    ProviderContext(attempt_id=attempt_id, generation=generation),
                    checkpoint,
                )
                if isinstance(decision, ReadToolDecision):
                    await self._control.read_tool(context, decision)
                elif isinstance(decision, PublishArtifactDecision):
                    await self._control.publish_artifact(context, decision)
                elif isinstance(decision, ProposeActionDecision):
                    await self._control.propose_action(context, decision)
                elif isinstance(decision, CompleteDecision):
                    await self._control.commit_final_checkpoint(context, decision)
                    return 0
                elif isinstance(decision, FailDecision):
                    await self._control.record_failure(context, decision)
                    return 1
            return 79
        except PlatformError as error:
            if error.code in (
                "STALE_GENERATION",
                "LEASE_EXPIRED",
                "LEASE_OWNER_MISMATCH",
                "VERSION_CONFLICT",
            ):
                return 75
            raise
