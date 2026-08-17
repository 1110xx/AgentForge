"""Standalone FastAPI application/router factory without global host state."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHttpException

from enterprise_agent_platform.contracts.errors import ApiErrorEnvelope
from enterprise_agent_platform.integration.host import HostPortError
from enterprise_agent_platform.persistence.protocol import PlatformError

from .dependencies import AgentPlatformContainer, request_trace_id
from .router import create_agent_platform_router


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ApiErrorEnvelope(
        schema_version="api-error/v1",
        code=code,
        message=message,
        trace_id=request_trace_id(request),
        retryable=retryable,
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=headers,
    )


def _host_status(error: HostPortError) -> int:
    if error.code == "UNAUTHENTICATED":
        return 401
    if error.code in {"FORBIDDEN", "HOST_CONTEXT_FORGED", "POLICY_DENIED"} or error.code.endswith(
        "DENIED"
    ):
        return 403
    if error.code == "NOT_FOUND":
        return 404
    if error.code == "PRECONDITION_REQUIRED":
        return 428
    if error.code == "INVALID_ETAG":
        return 400
    if error.code in {"REQUEST_VALIDATION_FAILED", "INVALID_OPAQUE_REFERENCE"}:
        return 422
    if error.code in {"HOST_PORT_UNAVAILABLE", "HOST_RESPONSE_INVALID"}:
        return 503
    return 500


def _platform_status(error: PlatformError) -> int:
    if error.code == "NOT_FOUND":
        return 404
    if error.code in {"ARTIFACT_ACCESS_DENIED", "FORBIDDEN"}:
        return 403
    if error.code in {
        "APPROVAL_DECISION_REJECTED",
        "EFFECT_ALREADY_PREPARED",
        "EFFECT_RECOVERY_STATE_INVALID",
        "EVENT_CURSOR_AHEAD",
        "IDEMPOTENCY_KEY_REUSED",
        "IDEMPOTENCY_IN_PROGRESS",
        "INVALID_STATE",
        "RESYNC_REQUIRED",
        "VERSION_CONFLICT",
        "STALE_UI_ACTION",
        "UI_ACTION_DIGEST_MISMATCH",
        "UI_ACTION_MISMATCH",
    }:
        return 409
    if error.code in {
        "INVALID_ARTIFACT_VERSION",
        "INVALID_EVENT_CURSOR",
        "INVALID_EVENT_LIMIT",
        "REQUEST_VALIDATION_FAILED",
        "UI_ACTION_NOT_SUPPORTED",
    }:
        return 422
    return 500


def create_agent_platform_app(container: AgentPlatformContainer) -> FastAPI:
    app = FastAPI(
        title="Enterprise Agent Platform API",
        version="0.1.0",
        openapi_version="3.1.0",
    )

    @app.exception_handler(HostPortError)
    async def host_error(request: Request, error: HostPortError) -> JSONResponse:
        status = _host_status(error)
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return _error_response(
            request,
            status_code=status,
            code=error.code if status != 500 else "INTERNAL_ERROR",
            message=error.message if status != 500 else "internal server error",
            retryable=error.retryable if status != 500 else False,
            headers=headers,
        )

    @app.exception_handler(PlatformError)
    async def platform_error(request: Request, error: PlatformError) -> JSONResponse:
        status = _platform_status(error)
        return _error_response(
            request,
            status_code=status,
            code=error.code if status != 500 else "INTERNAL_ERROR",
            message=error.message if status != 500 else "internal server error",
            retryable=error.retryable if status != 500 else False,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        missing_if_match = any(
            item.get("type") == "missing"
            and tuple(item.get("loc", ()))[-2:]
            in (("header", "If-Match"), ("header", "if-match"))
            for item in error.errors()
        )
        if missing_if_match:
            return _error_response(
                request,
                status_code=428,
                code="PRECONDITION_REQUIRED",
                message="If-Match is required",
            )
        return _error_response(
            request,
            status_code=422,
            code="REQUEST_VALIDATION_FAILED",
            message="request validation failed",
        )

    @app.exception_handler(StarletteHttpException)
    async def http_error(request: Request, error: StarletteHttpException) -> JSONResponse:
        code = "NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR"
        return _error_response(
            request,
            status_code=error.status_code,
            code=code,
            message="resource was not found" if error.status_code == 404 else "request failed",
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        del error
        return _error_response(
            request,
            status_code=500,
            code="INTERNAL_ERROR",
            message="internal server error",
        )

    app.include_router(create_agent_platform_router(container))

    def stable_openapi() -> dict[str, object]:
        if app.openapi_schema is None:
            document = get_openapi(
                title=app.title,
                version=app.version,
                openapi_version=app.openapi_version,
                routes=app.routes,
            )
            schemas = document.get("components", {}).get("schemas")
            if isinstance(schemas, dict):
                schemas.pop("HTTPValidationError", None)
                schemas.pop("ValidationError", None)
            app.openapi_schema = document
        return app.openapi_schema

    app.openapi = stable_openapi
    return app
