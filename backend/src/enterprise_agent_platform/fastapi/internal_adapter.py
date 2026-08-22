"""Adapter wiring the main app's services into :class:`InternalApiContainer`.

Mounted on the Control-Plane app so an HTTP Runtime transport (K8s Pod / Docker
runner, SDD §4.1/§7.1) can bootstrap and drive the **same parent-side op
transactions** that the pipe transport uses — the orchestrator op handlers are
transport-agnostic (they only touch store/control/checkpoints).

Token conventions (demo projection; production swaps real capability issuers —
K8s service-account projection / OIDC, see ``security/capabilities.py``):

* ``projected:{tenant_id}``  — bootstrap caller identity (host projection)
* ``runtime-token:{attempt_id}`` — child Runtime identity
* ``service-token:{name}``    — cross-service identity (effect-worker / reconciler)
* ``effect-token:{effect_id}`` — approved-effect execution capability
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from enterprise_agent_platform.contracts.enums import EffectState
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.domain.records import DispatchTicket
from enterprise_agent_platform.execution.pipe_transport import (
    OP_BOOTSTRAP,
    OP_COMMIT_CHECKPOINT,
    OP_COMMIT_FINAL,
    OP_HEARTBEAT,
    OP_MODEL_CALL,
    OP_PROPOSE_ACTION,
    OP_PUBLISH_ARTIFACT,
    OP_READ_TOOL,
    OP_RECORD_FAILURE,
    OP_RESTORE,
)
from enterprise_agent_platform.execution.subprocess_orchestrator import (
    SubprocessOrchestrator,
)
from enterprise_agent_platform.fastapi.internal import (
    BootstrapPort,
    BootstrapResponseModel,
    EffectExecutionPort,
    InternalApiContainer,
    RuntimeOperationsPort,
    RuntimeVerifier,
    ServiceIdentityVerifier,
    SurfacePublisherPort,
)
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore
from enterprise_agent_platform.security.capabilities import VerifiedRuntimeCapability
from enterprise_agent_platform.tools.durable_effects import ReconciledDurableEffect

_RUNTIME_TTL_SECONDS = 300


def _runtime_token(attempt_id: str) -> str:
    return f"runtime-token:{attempt_id}"


def _service_token(name: str) -> str:
    return f"service-token:{name}"


def _effect_token(effect_id: str) -> str:
    return f"effect-token:{effect_id}"


class _BootstrapAdapter(BootstrapPort):
    """Project a host bootstrap into a Runtime identity (token + facts).

    Beyond issuing the runtime token, the HTTP bootstrap **activates the
    Lease** through the shared control service — otherwise the Pod could
    never move Attempt CLAIMED / Run RUNNING and every heartbeat or turn
    checkpoint CAS would fail (the pipe transport activates it parent-side,
    SDD §6.1).
    """

    def __init__(
        self,
        store: PlatformStore,
        control: ControlPlaneService | None = None,
    ) -> None:
        self._store = store
        self._control = control

    async def claim(
        self,
        *,
        projected_token: str,
        request_pod_uid: str,
        attempt_id: str,
        generation: int,
    ) -> BootstrapResponseModel:
        if not projected_token.startswith("projected:"):
            raise PlatformError("AUTH_FAILED", "projected host token required")
        tenant_id = projected_token.removeprefix("projected:")
        if not tenant_id:
            raise PlatformError("AUTH_FAILED", "empty tenant in projected token")
        attempt = await self._store.get_attempt(tenant_id, attempt_id)
        if attempt.generation != generation:
            raise PlatformError("AUTH_FAILED", "attempt generation mismatch")
        lease_owner = f"http-runtime:{attempt_id}"
        lease_version = 0
        expires_at = ""
        if self._control is not None:
            ctx = RequestContext(
                tenant_id=tenant_id,
                actor_id=lease_owner,
                scopes=("runs:execute", "runs:write"),
                request_id=f"http-bootstrap:{attempt_id}",
                trace_id=f"trace:{attempt.run_id}",
            )
            lease = await self._control.activate_lease(
                ctx,
                attempt_id,
                generation,
                owner=lease_owner,
                expected_lease_version=1,
            )
            lease_version = lease.version
            expires_at = lease.expires_at.isoformat() if lease.expires_at else ""
        return BootstrapResponseModel(
            runtime_token=_runtime_token(attempt_id),
            tenant_id=attempt.tenant_id,
            run_id=attempt.run_id,
            execution_unit_id=attempt.execution_unit_id,
            attempt_id=attempt.attempt_id,
            generation=attempt.generation,
            lease_owner=lease_owner,
            lease_version=lease_version,
            expires_at=expires_at,
        )


class _RuntimeVerifierAdapter(RuntimeVerifier):
    """Verify a Runtime identity token against durable Attempt facts."""

    def __init__(self, store: PlatformStore) -> None:
        self._store = store

    async def verify_runtime(
        self,
        bearer: str,
        *,
        tenant_id: str,
        run_id: str,
        attempt_id: str,
        generation: int,
        required_scopes: tuple[str, ...],
    ) -> VerifiedRuntimeCapability:
        if bearer != _runtime_token(attempt_id):
            raise PlatformError("AUTH_FAILED", "runtime token mismatch")
        attempt = await self._store.get_attempt(tenant_id, attempt_id)
        if attempt.run_id != run_id or attempt.generation != generation:
            raise PlatformError("AUTH_FAILED", "runtime subject facts mismatch")
        now = datetime.now(UTC)
        return VerifiedRuntimeCapability(
            token_id=f"rt:{attempt_id}",
            issuer="control-plane",
            audience="runner",
            tenant_id=tenant_id,
            run_id=run_id,
            execution_unit_id=attempt.execution_unit_id,
            attempt_id=attempt_id,
            generation=generation,
            scopes=required_scopes,
            issued_at=now,
            expires_at=now + timedelta(seconds=_RUNTIME_TTL_SECONDS),
        )


class _RuntimeOpsAdapter(RuntimeOperationsPort):
    """Dispatch HTTP Runtime ops to the transport-agnostic orchestrator handlers.

    The orchestrator is reused as an op-service here (no pipes are spawned); it
    is constructed with the same store/control plus the host's
    ``run_sessions`` (real LLM proxy for model_call) and
    ``resource_resolver`` (real resource proxy for read_tool).
    """

    _OP_MAP: ClassVar[dict[str, str]] = {
        "bootstrap": OP_BOOTSTRAP,
        "restore": OP_RESTORE,
        "heartbeat": OP_HEARTBEAT,
        "read_tool": OP_READ_TOOL,
        "publish_artifact": OP_PUBLISH_ARTIFACT,
        "propose_action": OP_PROPOSE_ACTION,
        "commit_checkpoint": OP_COMMIT_CHECKPOINT,
        "commit_final_checkpoint": OP_COMMIT_FINAL,
        "record_failure": OP_RECORD_FAILURE,
        "model_call": OP_MODEL_CALL,
    }

    # Ops whose full result payload the Pod runtime needs (restore carries the
    # checkpoint cursor + agent snapshot; heartbeat the fresh lease_version;
    # read_tool the resolved content; model_call the LLM response). The rest
    # are normalised to InternalOperationResult{status, result_ref}.
    _PASSTHROUGH_OPS: ClassVar[frozenset[str]] = frozenset(
        {"restore", "heartbeat", "read_tool", "model_call"}
    )

    def __init__(
        self,
        store: PlatformStore,
        orchestrator: SubprocessOrchestrator | None = None,
        *,
        run_sessions=None,
        resource_resolver=None,
    ) -> None:
        self._orchestrator = orchestrator or SubprocessOrchestrator(
            store=store,
            control=ControlPlaneService(store),
            run_sessions=run_sessions,
            resource_resolver=resource_resolver,
        )
        self._store = store

    async def execute(
        self,
        operation: str,
        capability: VerifiedRuntimeCapability,
        request: Any,
    ) -> dict[str, Any]:
        op = self._OP_MAP.get(operation)
        if op is None:
            raise PlatformError("UNKNOWN_OPERATION", f"runtime op not supported: {operation}")
        ticket = DispatchTicket(
            worker_id=f"http-runtime:{capability.attempt_id}",
            tenant_id=capability.tenant_id,
            run_id=capability.run_id,
            execution_unit_id=capability.execution_unit_id,
            attempt_id=capability.attempt_id,
            lease_id="http-lease",
            generation=capability.generation,
            source_checkpoint_id="",
        )
        if op == OP_RESTORE:
            # The checkpoint cursor the Pod must rehydrate from is the unit's
            # current committed checkpoint — HTTP requests cannot know it upfront.
            unit = await self._store.get_execution_unit(
                capability.tenant_id, capability.execution_unit_id
            )
            ticket = replace(
                ticket,
                source_checkpoint_id=unit.current_checkpoint_id or "",
            )
        ctx = RequestContext(
            tenant_id=capability.tenant_id,
            actor_id=f"runtime:{capability.attempt_id}",
            scopes=capability.scopes,
            request_id=f"http-op:{capability.attempt_id}:{operation}",
            trace_id=f"trace:{capability.run_id}",
        )
        kwargs: dict[str, object] = {}
        if op in (OP_BOOTSTRAP, OP_RESTORE):
            kwargs["attempt_id"] = capability.attempt_id
            kwargs["generation"] = capability.generation
        if op in (OP_HEARTBEAT, OP_COMMIT_CHECKPOINT, OP_COMMIT_FINAL):
            # Bundle runtime facts under ``context`` — the shared op handlers
            # read lease identity/version from there (pipe frames do the same).
            kwargs["context"] = {
                "attempt_id": capability.attempt_id,
                "generation": capability.generation,
                "pod_uid": "",
                "runtime_token": f"runtime-token:{capability.attempt_id}",
                "lease_owner": getattr(request, "lease_owner", ""),
                "lease_version": getattr(request, "lease_version", 1),
            }
        if op == OP_HEARTBEAT:
            kwargs["attempt_id"] = capability.attempt_id
            kwargs["generation"] = capability.generation
        if op in (OP_COMMIT_CHECKPOINT, OP_COMMIT_FINAL):
            # Passthrough the Agent snapshot on both commit paths: turn-level
            # checkpoints are the mid-run boundaries, the final checkpoint is
            # the terminal snapshot a follow-up / rerun Attempt restores from
            # (SDD §5.5 restore hydration closure). The Pod runtime sends its
            # exported ``pi-agent-core/v1`` state on both.
            kwargs["agent_state"] = getattr(request, "agent_state", {})
            kwargs["agent_state_schema_version"] = getattr(
                request, "agent_state_schema_version", "pi-agent-core/v1"
            )
        if op == OP_READ_TOOL:
            kwargs["tool_name"] = getattr(request, "tool_name", "")
            kwargs["arguments_ref"] = getattr(request, "arguments_ref", "")
        if op == OP_PUBLISH_ARTIFACT:
            kwargs["workspace_path"] = getattr(request, "workspace_path", "")
            kwargs["logical_name"] = getattr(request, "logical_name", "")
            kwargs["classification"] = getattr(request, "classification", "general")
        if op == OP_PROPOSE_ACTION:
            kwargs["action_ref"] = getattr(request, "action_ref", "")
            kwargs["canonical_payload_ref"] = getattr(request, "canonical_payload_ref", "")
        if op == OP_COMMIT_FINAL:
            kwargs["summary"] = getattr(request, "summary", "Completed.")
        if op == OP_RECORD_FAILURE:
            kwargs["reason_code"] = getattr(request, "reason_code", "RUNTIME_FAILURE")
        if op == OP_MODEL_CALL:
            kwargs["model"] = getattr(request, "model", {})
            kwargs["system_prompt"] = getattr(request, "system_prompt", "")
            kwargs["messages"] = getattr(request, "messages", [])
            kwargs["tools"] = getattr(request, "tools", [])
            kwargs["options"] = getattr(request, "options", {})
        result = await self._orchestrator._handle(ticket, ctx, op, kwargs)
        if op in _RuntimeOpsAdapter._PASSTHROUGH_OPS:
            # Full payload for restore / heartbeat / read_tool / model_call.
            return result
        # Normalise the rest to InternalOperationResult{status, result_ref}:
        # the orchestrator handlers return richer shapes (artifact_id /
        # action_ref / checkpoint_id); the HTTP wire contract only carries
        # status + one ref (see fastapi/internal.py).
        result_ref = (
            result.get("artifact_id")
            or result.get("action_ref")
            or result.get("checkpoint_id")
            or None
        )
        return {"status": str(result.get("status", "ok")), "result_ref": result_ref}


class _ServiceIdentityAdapter(ServiceIdentityVerifier):
    """Verify cross-service bearer tokens (demo convention)."""

    async def verify(self, bearer: str | None, *, required_service: str) -> str:
        if bearer != _service_token(required_service):
            raise PlatformError("AUTH_FAILED", f"service token required ({required_service})")
        return required_service


class _EffectExecutionAdapter(EffectExecutionPort):
    """Authorize an approved-effect execution and advance the ledger."""

    def __init__(self, store: PlatformStore) -> None:
        self._store = store

    async def authorize_and_execute(
        self,
        tenant_id: str,
        effect_id: str,
        effect_token: str,
        executor_id: str,
    ) -> dict[str, Any]:
        if effect_token != _effect_token(effect_id):
            raise PlatformError("AUTH_FAILED", "effect capability token required")
        effect = await self._store.get_effect(tenant_id, effect_id)
        if effect.state is not EffectState.PREPARED:
            raise PlatformError("EFFECT_STATE", f"not executable from {effect.state.value}")
        now = datetime.now(UTC)
        executing = replace(
            effect,
            state=EffectState.EXECUTING,
            version=effect.version + 1,
            executor_id=executor_id,
            execution_epoch=effect.execution_epoch + 1,
            updated_at=now,
        )
        async with self._store.transaction() as tx:
            await tx.replace_effect_cas(executing, effect.version)
        return {
            "effect_id": effect_id,
            "state": executing.state.value,
            "version": executing.version,
        }

    async def authorize_and_reconcile(
        self,
        tenant_id: str,
        effect_id: str,
        result: ReconciledDurableEffect,
        effect_token: str,
    ) -> dict[str, Any]:
        if effect_token != _effect_token(effect_id):
            raise PlatformError("AUTH_FAILED", "effect capability token required")
        effect = await self._store.get_effect(tenant_id, effect_id)
        target = EffectState.SUCCEEDED if result.succeeded else EffectState.FAILED
        now = datetime.now(UTC)
        done = replace(
            effect,
            state=target,
            version=effect.version + 1,
            updated_at=now,
        )
        async with self._store.transaction() as tx:
            await tx.replace_effect_cas(done, effect.version)
        return {
            "effect_id": effect_id,
            "state": done.state.value,
            "version": done.version,
        }


def build_internal_container(
    store: PlatformStore,
    surface_publisher: SurfacePublisherPort,
    orchestrator: SubprocessOrchestrator | None = None,
    control: ControlPlaneService | None = None,
    *,
    run_sessions=None,
    resource_resolver=None,
) -> InternalApiContainer:
    """Build the Internal Runtime API container from main-app services.

    ``surface_publisher`` must be provided by the caller (e.g. a
    ``SurfaceServicePublisher`` wrapping the platform's UI surface service);
    the remaining ports are derived from the shared store + orchestrator.
    ``run_sessions`` / ``resource_resolver`` are threaded into the op-service
    so HTTP model_call / read_tool proxy the real provider + resolver.
    """
    return InternalApiContainer(
        bootstrap=_BootstrapAdapter(store, control or ControlPlaneService(store)),
        runtime_verifier=_RuntimeVerifierAdapter(store),
        runtime_operations=_RuntimeOpsAdapter(
            store,
            orchestrator
            or SubprocessOrchestrator(
                store=store,
                control=control or ControlPlaneService(store),
                run_sessions=run_sessions,
                resource_resolver=resource_resolver,
            ),
            run_sessions=run_sessions,
            resource_resolver=resource_resolver,
        ),
        surface_publisher=surface_publisher,
        service_identities=_ServiceIdentityAdapter(),
        effects=_EffectExecutionAdapter(store),
    )


__all__ = ["_runtime_token", "build_internal_container"]