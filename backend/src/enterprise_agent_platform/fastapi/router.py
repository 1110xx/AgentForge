"""Mountable V1 Run router with explicit public operations only."""
import hashlib
import json
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import Field

from enterprise_agent_platform.artifacts.downloads import ArtifactDownloadRequest
from enterprise_agent_platform.contracts.commands import (
    ChatCommand,
    CreateRunCommand,
    FollowupCommand,
    UiActionCommand,
)
from enterprise_agent_platform.contracts.errors import ApiErrorEnvelope
from enterprise_agent_platform.contracts.models import (
    ArtifactDownloadAuthorization,
    AttemptHistoryPage,
    FollowupAnswer,
    FollowupHistoryPage,
    RunEventPage,
    RunViewSnapshot,
    StrictModel,
    SurfaceRevision,
)
from enterprise_agent_platform.control.chat import classify_intent
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.effect_recovery import FailedEffectRecoveryService
from enterprise_agent_platform.control.views import RunQueryService
from enterprise_agent_platform.execution.session import SessionProviderError
from enterprise_agent_platform.integration.host import (
    HostPortError,
    resolve_run_authorization,
)
from enterprise_agent_platform.persistence.protocol import PlatformError

from .dependencies import AgentPlatformContainer, authenticate_request, require_scope
from .sse import stream_run_events

STRONG_ETAG = re.compile(r'"[1-9][0-9]*"$')


def _error_responses(*statuses: int) -> dict[int, dict[str, object]]:
    return {
        status: {"description": "Stable API error", "model": ApiErrorEnvelope}
        for status in statuses
    }


class CancelRunRequest(StrictModel):
    reason: Annotated[str | None, Field(max_length=1000)] = None


def _etag(version: int) -> str:
    return f'"{version}"'


def _if_match(value: str | None) -> int:
    if value is None:
        raise HostPortError("PRECONDITION_REQUIRED", "If-Match is required")
    match = STRONG_ETAG.fullmatch(value)
    if match is None:
        raise HostPortError("INVALID_ETAG", "If-Match must be a strong numeric ETag")
    return int(match.group(1))


def _idempotency_key(value: str | None) -> str:
    if value is None:
        raise HostPortError("REQUEST_VALIDATION_FAILED", "Idempotency-Key is required")
    return value


def _cancel_key(
    ctx: RequestContext,
    run_id: str,
    expected_version: int,
    reason: str | None,
) -> str:
    canonical = json.dumps(
        {
            "actor_id": ctx.actor_id,
            "expected_version": expected_version,
            "reason": reason,
            "run_id": run_id,
            "tenant_id": ctx.tenant_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"api-cancel:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _stream_cursor(header: str | None, query: str | None) -> int:
    if header is not None and query is not None and header != query:
        raise HostPortError(
            "REQUEST_VALIDATION_FAILED", "Last-Event-ID conflicts with after_event_seq"
        )
    raw = header if header is not None else query
    if raw is None:
        return 0
    if not raw.isdigit():
        raise HostPortError(
            "REQUEST_VALIDATION_FAILED", "event cursor must be a non-negative integer"
        )
    return int(raw)


def create_router(container: AgentPlatformContainer) -> APIRouter:
    """Public composition alias: mount the /v1 router on an adapter app."""
    return create_agent_platform_router(container)


def create_agent_platform_router(container: AgentPlatformContainer) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["runs"])
    query = RunQueryService(container.store)

    async def context(request: Request) -> RequestContext:
        return await authenticate_request(container, request)

    @router.post(
        "/runs",
        status_code=201,
        response_model=RunViewSnapshot,
        operation_id="createRun",
        responses=_error_responses(401, 403, 409, 422, 500, 503),
    )
    async def create_run(
        command: CreateRunCommand,
        response: Response,
        ctx: Annotated[RequestContext, Depends(context)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> RunViewSnapshot:
        require_scope(ctx, "runs:create")
        key = _idempotency_key(idempotency_key)
        authority = await resolve_run_authorization(
            ctx,
            command,
            resource_resolver=container.resource_resolver,
            host_context_verifier=container.host_context_verifier,
            policy_context_provider=container.policy_context_provider,
            timeout_seconds=container.host_port_timeout_seconds,
        )
        run = await container.control.create_run(
            ctx,
            command,
            key,
            authorization=authority,
        )
        # Log correlation: from this point, in-request logs carry the run_id so
        # Loki lines for this Run join to the Tempo spans (traces ↔ logs 一 key).
        from enterprise_agent_platform.platform.logging_json import set_run_id

        set_run_id(run.run_id)
        snapshot = await query.get_snapshot(ctx.tenant_id, run.run_id)
        response.headers["Location"] = f"/v1/runs/{run.run_id}"
        return snapshot

    @router.post(
        "/chat",
        status_code=201,
        response_model=RunViewSnapshot,
        operation_id="chatCreateRun",
        responses=_error_responses(401, 403, 409, 422, 500, 503),
    )
    async def chat_create_run(
        command: ChatCommand,
        response: Response,
        ctx: Annotated[RequestContext, Depends(context)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> RunViewSnapshot:
        """Free-form conversation entry (Phase 3.6 frontend launcher).

        Parses the natural-language message into an IntentPlan, then creates the
        Run through the exact same CreateRunCommand semantics as POST /runs
        (same idempotency, authorization and parameter-model guards). Follow-up
        questions continue through the existing followup chain.
        """
        require_scope(ctx, "runs:create")
        key = _idempotency_key(idempotency_key)
        plan = classify_intent(command.message, command.workflow_hint)
        run_command = CreateRunCommand(
            workflow_type=plan.workflow_type,
            intent=plan.intent,
            resource_refs=command.resource_refs,
            host_context_ref=command.host_context_ref,
        )
        authority = await resolve_run_authorization(
            ctx,
            run_command,
            resource_resolver=container.resource_resolver,
            host_context_verifier=container.host_context_verifier,
            policy_context_provider=container.policy_context_provider,
            timeout_seconds=container.host_port_timeout_seconds,
        )
        run = await container.control.create_run(
            ctx,
            run_command,
            key,
            authorization=authority,
        )
        snapshot = await query.get_snapshot(ctx.tenant_id, run.run_id)
        response.headers["Location"] = f"/v1/runs/{run.run_id}"
        return snapshot

    @router.get(
        "/runs/{run_id}",
        response_model=RunViewSnapshot,
        operation_id="getRun",
        responses=_error_responses(401, 403, 404, 422, 500),
    )
    async def get_run(
        run_id: str,
        response: Response,
        ctx: Annotated[RequestContext, Depends(context)],
    ) -> RunViewSnapshot:
        require_scope(ctx, "runs:read")
        snapshot = await query.get_snapshot(ctx.tenant_id, run_id)
        response.headers["ETag"] = _etag(snapshot.view.version)
        return snapshot

    @router.get(
        "/runs/{run_id}/attempts",
        response_model=AttemptHistoryPage,
        operation_id="listRunAttempts",
        responses=_error_responses(401, 403, 404, 500),
    )
    async def list_run_attempts(
        run_id: str,
        ctx: Annotated[RequestContext, Depends(context)],
    ) -> AttemptHistoryPage:
        """Return the full Attempt history for a Run."""
        require_scope(ctx, "runs:read")
        return await query.list_attempts(ctx.tenant_id, run_id)

    @router.get(
        "/runs/{run_id}/events",
        response_model=RunEventPage,
        operation_id="listRunEvents",
        responses=_error_responses(401, 403, 404, 409, 422, 500),
    )
    async def list_run_events(
        run_id: str,
        ctx: Annotated[RequestContext, Depends(context)],
        after_event_seq: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> RunEventPage:
        require_scope(ctx, "runs:read")
        return await query.get_events(
            ctx.tenant_id,
            run_id,
            after_event_seq=after_event_seq,
            limit=limit,
        )

    @router.get(
        "/runs/{run_id}/events/stream",
        operation_id="streamRunEvents",
        response_class=StreamingResponse,
        responses=_error_responses(401, 403, 404, 409, 422, 500),
    )
    async def stream_events(
        request: Request,
        run_id: str,
        ctx: Annotated[RequestContext, Depends(context)],
        after_event_seq: Annotated[str | None, Query()] = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        require_scope(ctx, "runs:read")
        cursor = _stream_cursor(last_event_id, after_event_seq)
        await query.get_events(ctx.tenant_id, run_id, after_event_seq=cursor, limit=1)
        stream = stream_run_events(
            request=request,
            query=query,
            notifier=container.notifier,
            tenant_id=ctx.tenant_id,
            run_id=run_id,
            after_event_seq=cursor,
            trace_id=ctx.trace_id,
            heartbeat_seconds=container.sse_heartbeat_seconds,
            max_lifetime_seconds=container.sse_max_lifetime_seconds,
            batch_size=container.sse_batch_size,
        )
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post(
        "/runs/{run_id}/cancel",
        status_code=202,
        response_model=RunViewSnapshot,
        operation_id="cancelRun",
        responses=_error_responses(400, 401, 403, 404, 409, 422, 428, 500),
    )
    async def cancel_run(
        run_id: str,
        response: Response,
        ctx: Annotated[RequestContext, Depends(context)],
        if_match: Annotated[str, Header(alias="If-Match")],
        command: CancelRunRequest | None = None,
    ) -> RunViewSnapshot:
        require_scope(ctx, "runs:cancel")
        command = command or CancelRunRequest()
        expected_version = _if_match(if_match)
        run = await container.control.request_cancel(
            ctx,
            run_id,
            expected_version,
            _cancel_key(ctx, run_id, expected_version, command.reason),
            reason=command.reason,
        )
        snapshot = await query.get_snapshot(ctx.tenant_id, run.run_id)
        response.headers["ETag"] = _etag(snapshot.view.version)
        return snapshot

    @router.post(
        "/runs/{run_id}/reruns",
        status_code=201,
        response_model=RunViewSnapshot,
        operation_id="rerunRun",
        responses=_error_responses(400, 401, 403, 404, 409, 422, 428, 500),
    )
    async def rerun(
        run_id: str,
        response: Response,
        ctx: Annotated[RequestContext, Depends(context)],
        if_match: Annotated[str, Header(alias="If-Match")],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> RunViewSnapshot:
        require_scope(ctx, "runs:create")
        expected_version = _if_match(if_match)
        key = _idempotency_key(idempotency_key)
        run = await container.control.rerun(
            ctx,
            run_id,
            idempotency_key=key,
            expected_parent_version=expected_version,
        )
        snapshot = await query.get_snapshot(ctx.tenant_id, run.run_id)
        response.headers["Location"] = f"/v1/runs/{run.run_id}"
        response.headers["ETag"] = _etag(snapshot.view.version)
        return snapshot

    @router.post(
        "/runs/{run_id}/effects/{effect_id}/recover",
        status_code=202,
        response_model=RunViewSnapshot,
        operation_id="recoverFailedEffect",
        responses=_error_responses(400, 401, 403, 404, 409, 422, 428, 500),
    )
    async def recover_failed_effect(
        run_id: str,
        effect_id: str,
        response: Response,
        ctx: Annotated[RequestContext, Depends(context)],
        if_match: Annotated[str, Header(alias="If-Match")],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> RunViewSnapshot:
        require_scope(ctx, "effects:recover")
        await FailedEffectRecoveryService(container.store).recover(
            ctx,
            run_id=run_id,
            effect_id=effect_id,
            expected_run_version=_if_match(if_match),
            idempotency_key=_idempotency_key(idempotency_key),
        )
        snapshot = await query.get_snapshot(ctx.tenant_id, run_id)
        response.headers["ETag"] = _etag(snapshot.view.version)
        return snapshot

    @router.get(
        "/runs/{run_id}/surfaces/{surface_id}",
        response_model=SurfaceRevision,
        operation_id="getSurfaceRevision",
        responses=_error_responses(401, 403, 404, 409, 422, 500),
    )
    async def get_surface_revision(
        run_id: str,
        surface_id: str,
        ctx: Annotated[RequestContext, Depends(context)],
        revision: Annotated[int | None, Query(ge=1)] = None,
    ) -> SurfaceRevision:
        """Read one immutable A2UI surface revision (defaults to the latest)."""
        require_scope(ctx, "runs:read")
        return await query.get_surface_revision(
            ctx.tenant_id,
            run_id,
            surface_id,
            revision=revision,
        )

    @router.post(
        "/runs/{run_id}/actions",
        status_code=202,
        response_model=RunViewSnapshot,
        operation_id="submitRunUiAction",
        responses=_error_responses(401, 403, 404, 409, 422, 500, 503),
    )
    async def submit_ui_action(
        run_id: str,
        command: UiActionCommand,
        ctx: Annotated[RequestContext, Depends(context)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> RunViewSnapshot:
        require_scope(ctx, "runs:act")
        key = _idempotency_key(idempotency_key)
        if command.run_id != run_id or command.client_action_id != key:
            raise HostPortError(
                "REQUEST_VALIDATION_FAILED", "action identity does not match the request"
            )
        if container.ui_actions is None:
            raise HostPortError(
                "HOST_PORT_UNAVAILABLE",
                "UI action handling is not configured",
                retryable=True,
            )
        await container.ui_actions.handle(ctx, command, idempotency_key=key)
        return await query.get_snapshot(ctx.tenant_id, run_id)

    @router.post(
        "/runs/{run_id}/followups",
        response_model=FollowupAnswer,
        operation_id="submitFollowup",
        responses=_error_responses(401, 403, 404, 422, 500, 503),
    )
    async def submit_followup(
        run_id: str,
        command: FollowupCommand,
        ctx: Annotated[RequestContext, Depends(context)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> FollowupAnswer:
        """Append a read-only follow-up question to the Run's model session."""
        require_scope(ctx, "runs:read")
        key = _idempotency_key(idempotency_key)
        if command.run_id != run_id or command.client_followup_id != idempotency_key:
            raise HostPortError(
                "REQUEST_VALIDATION_FAILED", "follow-up identity does not match the request"
            )
        if container.followups is None:
            raise HostPortError(
                "HOST_PORT_UNAVAILABLE",
                "follow-up handling is not configured",
                retryable=True,
            )
        try:
            return await container.followups.followup(ctx, run_id, command, idempotency_key=key)
        except SessionProviderError as e:
            raise PlatformError(e.code, e.message) from e

    @router.get(
        "/runs/{run_id}/followups",
        response_model=FollowupHistoryPage,
        operation_id="listFollowups",
        responses=_error_responses(401, 403, 404, 422, 500),
    )
    async def list_followups(
        run_id: str,
        ctx: Annotated[RequestContext, Depends(context)],
    ) -> FollowupHistoryPage:
        """Return the follow-up history for a Run."""
        require_scope(ctx, "runs:read")
        if container.followups is None:
            raise HostPortError(
                "HOST_PORT_UNAVAILABLE",
                "follow-up handling is not configured",
                retryable=True,
            )
        try:
            return await container.followups.list_followups(ctx, run_id)
        except SessionProviderError as e:
            raise PlatformError(e.code, e.message) from e

    @router.get(
        "/runs/{run_id}/artifacts/{artifact_id}/versions/{version}/download-authorization",
        response_model=ArtifactDownloadAuthorization,
        operation_id="authorizeArtifactDownload",
        responses=_error_responses(401, 403, 404, 422, 500, 503),
    )
    async def authorize_artifact_download(
        run_id: str,
        artifact_id: str,
        version: Annotated[int, Path(ge=1)],
        ctx: Annotated[RequestContext, Depends(context)],
    ) -> ArtifactDownloadAuthorization:
        require_scope(ctx, "artifacts:download")
        if container.artifact_downloads is None:
            raise HostPortError(
                "HOST_PORT_UNAVAILABLE",
                "Artifact download authorization is not configured",
                retryable=True,
            )
        return await container.artifact_downloads.authorize(
            ctx,
            ArtifactDownloadRequest(
                run_id=run_id,
                artifact_id=artifact_id,
                version=version,
            ),
        )

    return router
