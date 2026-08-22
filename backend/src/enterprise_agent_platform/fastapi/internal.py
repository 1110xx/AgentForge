"""Private Runtime/Effect API with audience-bound capability authentication."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Protocol
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field, JsonValue

from enterprise_agent_platform.contracts.errors import ApiErrorEnvelope
from enterprise_agent_platform.contracts.models import StrictModel, SurfaceRevision
from enterprise_agent_platform.persistence.protocol import PlatformError
from enterprise_agent_platform.security.bootstrap import BootstrapResponse
from enterprise_agent_platform.security.capabilities import VerifiedRuntimeCapability
from enterprise_agent_platform.tools.durable_effects import ReconciledDurableEffect
from enterprise_agent_platform.ui.service import SurfaceCommitRequest, SurfaceService

MAX_AUTHORIZATION_BYTES = 32 * 1024


class BootstrapPort(Protocol):
    async def claim(
        self,
        projected_token: str,
        request_pod_uid: str,
        attempt_id: str,
        generation: int,
    ) -> BootstrapResponse: ...


class RuntimeVerifier(Protocol):
    async def verify_runtime(
        self,
        token: str,
        tenant_id: str,
        run_id: str,
        attempt_id: str,
        generation: int,
        required_scopes: tuple[str, ...],
    ) -> VerifiedRuntimeCapability: ...


class RuntimeOperationsPort(Protocol):
    async def execute(
        self,
        operation: str,
        capability: VerifiedRuntimeCapability,
        request: StrictModel,
    ) -> dict[str, JsonValue]: ...


class SurfacePublisherPort(Protocol):
    async def publish(self, request: SurfaceCommitRequest) -> SurfaceRevision: ...


class ServiceIdentityVerifier(Protocol):
    async def verify(self, token: str, *, required_service: str) -> str: ...


class EffectExecutionPort(Protocol):
    """Validate the Effect audience against stored effect facts."""

    async def authorize_and_execute(
        self,
        tenant_id: str,
        effect_id: str,
        effect_token: str,
        executor_id: str,
    ) -> dict[str, JsonValue]: ...

    async def authorize_and_reconcile(
        self,
        tenant_id: str,
        effect_id: str,
        result: ReconciledDurableEffect,
        reconciliation_token: str,
    ) -> dict[str, JsonValue]: ...


@dataclass(frozen=True, slots=True)
class InternalApiContainer:
    bootstrap: BootstrapPort
    runtime_verifier: RuntimeVerifier
    runtime_operations: RuntimeOperationsPort
    surface_publisher: SurfacePublisherPort
    service_identities: ServiceIdentityVerifier
    effects: EffectExecutionPort


class SurfaceServicePublisher:
    def __init__(self, service: SurfaceService) -> None:
        self._service = service

    async def publish(self, request: SurfaceCommitRequest) -> SurfaceRevision:
        published = await self._service.commit_revision(request)
        return await self._service.revision_contract(
            request.tenant_id, request.surface_id, published.revision
        )


class BootstrapRequest(StrictModel):
    pod_uid: Annotated[str, Field(min_length=1, max_length=255)]
    attempt_id: Annotated[str, Field(min_length=1, max_length=255)]
    generation: Annotated[int, Field(ge=1)]


class BootstrapResponseModel(StrictModel):
    runtime_token: str
    tenant_id: str
    run_id: str
    execution_unit_id: str
    attempt_id: str
    generation: Annotated[int, Field(ge=1)]
    # Lease facts returned once the HTTP bootstrap activates the Lease
    # (RESERVED → ACTIVE, Attempt → CLAIMED, Run → RUNNING). The Pod runtime
    # needs them as CAS prerequisites for heartbeat/turn checkpoints.
    lease_owner: str = ""
    lease_version: int = 0
    expires_at: str = ""


class RuntimeSubjectRequest(StrictModel):
    tenant_id: Annotated[str, Field(min_length=1, max_length=255)]
    run_id: Annotated[str, Field(min_length=1, max_length=255)]
    attempt_id: Annotated[str, Field(min_length=1, max_length=255)]
    generation: Annotated[int, Field(ge=1)]


class RestoreRequest(RuntimeSubjectRequest):
    # The Pod's HTTP runtime signs every op with the full subject it was
    # granted at bootstrap (http_runtime._subject()); the Internal API must
    # accept the unit id as part of the wire contract (extra="forbid" would
    # otherwise reject it).
    execution_unit_id: Annotated[str, Field(min_length=1, max_length=255)]
    lease_owner: Annotated[str, Field(min_length=1, max_length=255)]
    lease_version: Annotated[int, Field(ge=1)]


class HeartbeatRequest(RestoreRequest):
    pass


class TurnCheckpointRequest(RestoreRequest):
    """Turn-level (mid-run) Checkpoint commit carrying the Agent snapshot.

    Same runtime subject + lease facts as HeartbeatRequest, plus the
    pi-agent-core Agent state snapshot persisted at TurnEnd (the safe
    checkpoint boundary, SDD §5.5).
    """

    agent_state: dict[str, JsonValue] = Field(default_factory=dict)
    agent_state_schema_version: Annotated[
        str, Field(min_length=1, max_length=64)
    ] = "pi-agent-core/v1"


class ModelCallRequest(RuntimeSubjectRequest):
    """A full LLM call proxied from the Pod runtime (HttpStream).

    Mirrors the PipeStream wire contract: complete message history + tool
    definitions + sampling options; the Control Plane proxies to the real
    provider through its RunSessionProvider and returns non-streaming content
    blocks (text / tool_use / thinking).

    ``execution_unit_id`` is part of the bootstrap-granted subject the Pod
    always signs with (see RestoreRequest).
    """

    execution_unit_id: Annotated[str, Field(min_length=1, max_length=255)]
    model: dict[str, JsonValue] = Field(default_factory=dict)
    system_prompt: str = ""
    messages: list[dict[str, JsonValue]] = Field(default_factory=list)
    tools: list[dict[str, JsonValue]] = Field(default_factory=list)
    options: dict[str, JsonValue] = Field(default_factory=dict)


class ReadToolRequest(RuntimeSubjectRequest):
    tool_name: Annotated[str, Field(min_length=1, max_length=255)]
    arguments_ref: Annotated[str, Field(min_length=1, max_length=1024)]


class PublishArtifactRequest(RuntimeSubjectRequest):
    workspace_path: Annotated[str, Field(min_length=1, max_length=1024)]
    logical_name: Annotated[str, Field(min_length=1, max_length=255)]
    classification: Annotated[str, Field(min_length=1, max_length=64)]


class ProposeActionRequest(RuntimeSubjectRequest):
    action_ref: Annotated[str, Field(min_length=1, max_length=255)]
    canonical_payload_ref: Annotated[str, Field(min_length=1, max_length=1024)]


class FinalCheckpointRequest(RestoreRequest):
    """Terminal checkpoint commit: runtime subject + lease facts + summary.

    Extends ``RestoreRequest`` so the Pod can keep signing its CAS writes with
    the fresh lease facts (owner/version) — the shared commit handler reads
    them from the ``context`` bundle. ``agent_state`` is accepted for forward
    compatibility; the HTTP transport currently streams Agent snapshots via
    turn-level checkpoints instead of inlining them here.
    """

    summary: Annotated[str, Field(min_length=1, max_length=8192)]
    agent_state: dict[str, JsonValue] = Field(default_factory=dict)
    agent_state_schema_version: Annotated[
        str, Field(min_length=1, max_length=64)
    ] = "http-runtime/v0"


class RuntimeFailureRequest(RuntimeSubjectRequest):
    reason_code: Annotated[str, Field(min_length=1, max_length=128)]
    retryable: bool


class SurfacePublishRequest(RuntimeSubjectRequest):
    catalog_id: Annotated[str, Field(min_length=1, max_length=128)]
    protocol_version: Annotated[str, Field(min_length=1, max_length=64)]
    document: dict[str, JsonValue]


class InternalOperationResult(StrictModel):
    status: str
    result_ref: str | None = None


class RestoreResponse(StrictModel):
    """Full checkpoint payload for the Pod runner to rehydrate its Agent."""

    status: str = "ok"
    checkpoint_id: str
    checkpoint_state: str
    snapshot_state: str | None = None
    workflow_cursor: dict[str, JsonValue] = Field(default_factory=dict)
    agent_state: dict[str, JsonValue] = Field(default_factory=dict)
    agent_state_schema_version: str | None = None


class HeartbeatResponse(StrictModel):
    """Refreshed RuntimeContext (the new lease_version is a CAS prerequisite)."""

    attempt_id: str
    generation: int
    pod_uid: str
    runtime_token: str
    lease_owner: str
    lease_version: Annotated[int, Field(ge=1)]


class ReadToolResponse(StrictModel):
    """Resource proxy result returned to the Pod's remote_read_tool."""

    tool_name: str
    resource_ref: str
    resolved: dict[str, JsonValue] | None = None
    content: str


class ModelCallResponse(StrictModel):
    """Non-streaming LLM response (content blocks + stop_reason + usage).

    The Pod's HttpStream translates this back into
    ``AsyncIterator[AssistantMessageEvent]`` for pi-agent-core (same shape the
    pipe stream emits).
    """

    content: list[dict[str, JsonValue]] = Field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: dict[str, JsonValue] = Field(default_factory=dict)


class EffectExecutionResponse(StrictModel):
    effect_id: str
    state: str
    version: Annotated[int, Field(ge=1)]


class EffectReconciliationRequest(StrictModel):
    succeeded: bool
    remote_operation_id: Annotated[str | None, Field(max_length=255)] = None
    result: dict[str, JsonValue]
    evidence_ref: Annotated[str, Field(min_length=1, max_length=1024)]
    evidence_digest: Annotated[str, Field(min_length=1, max_length=128)]


def _bearer(value: str | None) -> str:
    if value is None or not value.startswith("Bearer "):
        raise PlatformError("UNAUTHENTICATED", "authentication failed")
    token = value.removeprefix("Bearer ").strip()
    if (
        not token
        or token != token.strip()
        or any(character.isspace() for character in token)
        or len(token.encode()) > MAX_AUTHORIZATION_BYTES
    ):
        raise PlatformError("UNAUTHENTICATED", "authentication failed")
    return token


def _opaque_token(value: str | None) -> str:
    if (
        value is None
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
        or len(value.encode()) > MAX_AUTHORIZATION_BYTES
    ):
        raise PlatformError("UNAUTHENTICATED", "authentication failed")
    return value


async def _runtime_identity(
    container: InternalApiContainer,
    authorization: str | None,
    request: RuntimeSubjectRequest,
    scope: str,
) -> VerifiedRuntimeCapability:
    return await container.runtime_verifier.verify_runtime(
        _bearer(authorization),
        tenant_id=request.tenant_id,
        run_id=request.run_id,
        attempt_id=request.attempt_id,
        generation=request.generation,
        required_scopes=(scope,),
    )


def create_internal_router(container: InternalApiContainer) -> APIRouter:
    router = APIRouter(prefix="/internal/v1", tags=["internal-runtime"])

    @router.post(
        "/runtime/bootstrap",
        response_model=BootstrapResponseModel,
        operation_id="claimRuntimeBootstrap",
    )
    async def bootstrap(
        command: BootstrapRequest,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> BootstrapResponse:
        return await container.bootstrap.claim(
            projected_token=_bearer(authorization),
            request_pod_uid=command.pod_uid,
            attempt_id=command.attempt_id,
            generation=command.generation,
        )

    async def execute_runtime(
        *,
        operation: str,
        scope: str,
        command: RuntimeSubjectRequest,
        authorization: str | None,
    ) -> dict[str, JsonValue]:
        capability = await _runtime_identity(container, authorization, command, scope)
        return await container.runtime_operations.execute(operation, capability, command)

    @router.post(
        "/runtime/restore",
        response_model=RestoreResponse,
        operation_id="restoreRuntime",
    )
    async def restore(
        command: RestoreRequest,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> dict[str, JsonValue]:
        return await execute_runtime(
            operation="restore",
            scope="runtime:restore",
            command=command,
            authorization=authorization,
        )

    @router.post(
        "/runtime/heartbeat",
        response_model=HeartbeatResponse,
        operation_id="heartbeatRuntime",
    )
    async def heartbeat(
        command: HeartbeatRequest,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> dict[str, JsonValue]:
        return await execute_runtime(
            operation="heartbeat",
            scope="runtime:heartbeat",
            command=command,
            authorization=authorization,
        )

    @router.post(
        "/runtime/tools/read",
        response_model=ReadToolResponse,
        operation_id="invokeRuntimeReadTool",
    )
    async def read_tool(
        command: ReadToolRequest,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> dict[str, JsonValue]:
        return await execute_runtime(
            operation="read_tool",
            scope="tool:invoke",
            command=command,
            authorization=authorization,
        )

    @router.post(
        "/runtime/checkpoints",
        response_model=InternalOperationResult,
        operation_id="commitRuntimeTurnCheckpoint",
    )
    async def commit_checkpoint(
        command: TurnCheckpointRequest,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> dict[str, JsonValue]:
        return await execute_runtime(
            operation="commit_checkpoint",
            scope="checkpoint:commit",
            command=command,
            authorization=authorization,
        )

    @router.post(
        "/runtime/model-call",
        response_model=ModelCallResponse,
        operation_id="proxyRuntimeModelCall",
    )
    async def model_call(
        command: ModelCallRequest,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> dict[str, JsonValue]:
        return await execute_runtime(
            operation="model_call",
            scope="model:call",
            command=command,
            authorization=authorization,
        )

    @router.post(
        "/runtime/artifacts",
        response_model=InternalOperationResult,
        operation_id="publishRuntimeArtifact",
    )
    async def publish_artifact(
        command: PublishArtifactRequest,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> dict[str, JsonValue]:
        return await execute_runtime(
            operation="publish_artifact",
            scope="artifact:publish",
            command=command,
            authorization=authorization,
        )

    @router.post(
        "/runtime/action-proposals",
        response_model=InternalOperationResult,
        operation_id="proposeRuntimeAction",
    )
    async def propose_action(
        command: ProposeActionRequest,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> dict[str, JsonValue]:
        return await execute_runtime(
            operation="propose_action",
            scope="action:propose",
            command=command,
            authorization=authorization,
        )

    @router.post(
        "/runtime/checkpoints/final",
        response_model=InternalOperationResult,
        operation_id="commitRuntimeFinalCheckpoint",
    )
    async def commit_final_checkpoint(
        command: FinalCheckpointRequest,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> dict[str, JsonValue]:
        return await execute_runtime(
            operation="commit_final_checkpoint",
            scope="checkpoint:commit",
            command=command,
            authorization=authorization,
        )

    @router.post(
        "/runtime/failures",
        response_model=InternalOperationResult,
        operation_id="recordRuntimeFailure",
    )
    async def record_failure(
        command: RuntimeFailureRequest,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> dict[str, JsonValue]:
        return await execute_runtime(
            operation="record_failure",
            scope="runtime:fail",
            command=command,
            authorization=authorization,
        )

    @router.post(
        "/runtime/ui-surfaces/{surface_id}/revisions",
        status_code=201,
        response_model=SurfaceRevision,
        operation_id="publishRuntimeSurfaceRevision",
    )
    async def publish_surface(
        surface_id: str,
        command: SurfacePublishRequest,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> SurfaceRevision:
        capability = await _runtime_identity(container, authorization, command, "ui:publish")
        return await container.surface_publisher.publish(
            SurfaceCommitRequest(
                tenant_id=capability.tenant_id,
                run_id=capability.run_id,
                surface_id=surface_id,
                source_attempt_id=capability.attempt_id,
                source_generation=capability.generation,
                catalog_id=command.catalog_id,
                protocol_version=command.protocol_version,
                document=command.document,
                trace_id=getattr(request.state, "trace_id", None),
            )
        )

    @router.post(
        "/tenants/{tenant_id}/effects/{effect_id}/execute",
        response_model=EffectExecutionResponse,
        operation_id="executeApprovedEffect",
    )
    async def execute_effect(
        tenant_id: str,
        effect_id: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        effect_capability: Annotated[str | None, Header(alias="X-Effect-Capability")] = None,
    ) -> dict[str, JsonValue]:
        executor_id = await container.service_identities.verify(
            _bearer(authorization), required_service="effect-worker"
        )
        return await container.effects.authorize_and_execute(
            tenant_id,
            effect_id,
            _opaque_token(effect_capability),
            executor_id=executor_id,
        )

    @router.post(
        "/tenants/{tenant_id}/effects/{effect_id}/reconcile",
        response_model=EffectExecutionResponse,
        operation_id="reconcileApprovedEffect",
    )
    async def reconcile_effect(
        tenant_id: str,
        effect_id: str,
        command: EffectReconciliationRequest,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        reconciliation_capability: Annotated[
            str | None, Header(alias="X-Reconciliation-Capability")
        ] = None,
    ) -> dict[str, JsonValue]:
        await container.service_identities.verify(
            _bearer(authorization), required_service="effect-reconciler"
        )
        return await container.effects.authorize_and_reconcile(
            tenant_id,
            effect_id,
            ReconciledDurableEffect(
                succeeded=command.succeeded,
                remote_operation_id=command.remote_operation_id,
                result=dict(command.result),
                evidence_ref=command.evidence_ref,
                evidence_digest=command.evidence_digest,
            ),
            _opaque_token(reconciliation_capability),
        )

    return router


def _status(error: PlatformError) -> int:
    if error.code in {
        "UNAUTHENTICATED",
        "CAPABILITY_EXPIRED",
        "CAPABILITY_NOT_YET_VALID",
        "CAPABILITY_REVOKED",
        "BOOTSTRAP_IDENTITY_REJECTED",
        "BOOTSTRAP_IDENTITY_MISMATCH",
        "SERVICE_IDENTITY_REJECTED",
    }:
        return 401
    if error.code in {
        "CAPABILITY_AUDIENCE_MISMATCH",
        "CAPABILITY_ISSUER_MISMATCH",
        "CAPABILITY_SCOPE_DENIED",
        "CAPABILITY_SUBJECT_MISMATCH",
        "STALE_GENERATION",
    }:
        return 403
    if error.code in {
        "BOOTSTRAP_ALREADY_CONSUMED",
        "EFFECT_ALREADY_PREPARED",
        "EFFECT_EXECUTOR_FENCE_MISMATCH",
        "EFFECT_EXECUTOR_STILL_ACTIVE",
        "EFFECT_NOT_EXECUTABLE",
        "EFFECT_RECOVERY_STATE_INVALID",
        "EFFECT_STATE_CONFLICT",
        "RECONCILIATION_FENCE_REQUIRED",
        "VERSION_CONFLICT",
    }:
        return 409
    if error.code in {
        "EFFECT_PAYLOAD_MISMATCH",
        "RECONCILIATION_EVIDENCE_INVALID",
    }:
        return 422
    if error.code == "NOT_FOUND":
        return 404
    return 500


def create_internal_api_app(container: InternalApiContainer) -> FastAPI:
    app = FastAPI(title="Enterprise Agent Platform Internal API", version="0.1.0")

    @app.middleware("http")
    async def bind_trace(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.trace_id = uuid4().hex
        return await call_next(request)

    @app.exception_handler(PlatformError)
    async def platform_error(request: Request, error: PlatformError) -> JSONResponse:
        status = _status(error)
        public_code = error.code if status != 500 else "INTERNAL_ERROR"
        body = ApiErrorEnvelope(
            schema_version="api-error/v1",
            code=public_code,
            message=(
                "authentication failed"
                if status in {401, 403}
                else (error.message if status != 500 else "internal server error")
            ),
            trace_id=request.state.trace_id,
            retryable=error.retryable if status != 500 else False,
        )
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return JSONResponse(status_code=status, content=body.model_dump(mode="json"), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        del error
        body = ApiErrorEnvelope(
            schema_version="api-error/v1",
            code="REQUEST_VALIDATION_FAILED",
            message="request validation failed",
            trace_id=request.state.trace_id,
            retryable=False,
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        del error
        body = ApiErrorEnvelope(
            schema_version="api-error/v1",
            code="INTERNAL_ERROR",
            message="internal server error",
            trace_id=request.state.trace_id,
            retryable=False,
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    app.include_router(create_internal_router(container))
    return app


__all__ = [
    "InternalApiContainer",
    "SurfaceServicePublisher",
    "create_internal_api_app",
    "create_internal_router",
]
