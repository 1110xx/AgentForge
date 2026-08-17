"""Request-scoped trusted context resolution for the mountable FastAPI router."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from fastapi import Request

from enterprise_agent_platform.artifacts.downloads import ArtifactDownloadRequest
from enterprise_agent_platform.contracts.commands import UiActionCommand
from enterprise_agent_platform.contracts.models import ArtifactDownloadAuthorization
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.integration.host import (
    AuthContextProvider,
    HostContextVerifier,
    HostPortError,
    PolicyContextProvider,
    ResourceResolver,
)
from enterprise_agent_platform.persistence.protocol import PlatformStore

from .sse import PollingRunEventNotifier, RunEventNotifier

SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9.:]{1,128}$")
TRACEPARENT = re.compile(
    r"[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$", re.IGNORECASE
)


class UiActionHandler(Protocol):
    async def handle(
        self,
        ctx: RequestContext,
        command: UiActionCommand,
        idempotency_key: str,
    ) -> None: ...


class ArtifactDownloadAuthorizer(Protocol):
    async def authorize(
        self, ctx: RequestContext, request: ArtifactDownloadRequest
    ) -> ArtifactDownloadAuthorization: ...


@dataclass(frozen=True, slots=True)
class AgentPlatformContainer:
    store: PlatformStore
    control: ControlPlaneService
    auth_context_provider: AuthContextProvider
    resource_resolver: ResourceResolver
    host_context_verifier: HostContextVerifier
    policy_context_provider: PolicyContextProvider
    notifier: RunEventNotifier = field(default_factory=PollingRunEventNotifier)
    host_port_timeout_seconds: float = 2.0
    sse_heartbeat_seconds: float = 15.0
    sse_max_lifetime_seconds: float = 300.0
    sse_batch_size: int = 100
    ui_actions: UiActionHandler | None = None
    artifact_downloads: ArtifactDownloadAuthorizer | None = None

    def __post_init__(self) -> None:
        if self.host_port_timeout_seconds <= 0:
            raise ValueError("host port timeout must be positive")
        if self.sse_heartbeat_seconds <= 0 or self.sse_max_lifetime_seconds <= 0:
            raise ValueError("SSE timing values must be positive")
        if not 1 <= self.sse_batch_size <= 100:
            raise ValueError("SSE batch size must be between 1 and 100")


def request_trace_id(request: Request) -> str:
    existing = getattr(request.state, "trace_id", None)
    if isinstance(existing, str) and existing:
        return existing
    match = TRACEPARENT.fullmatch(request.headers.get("traceparent", ""))
    trace_id = match.group(1).lower() if match else uuid4().hex
    request.state.trace_id = trace_id
    return trace_id


async def authenticate_request(
    container: AgentPlatformContainer, request: Request
) -> RequestContext:
    if request.headers.get("x-tenant-id") is not None:
        raise HostPortError(
            "REQUEST_VALIDATION_FAILED", "tenant identity cannot be supplied by the client"
        )
    supplied_request_id = request.headers.get("x-request-id")
    request_id = (
        supplied_request_id
        if supplied_request_id is not None and SAFE_REQUEST_ID.fullmatch(supplied_request_id)
        else f"request_{uuid4().hex}"
    )
    trace_id = request_trace_id(request)
    try:
        ctx = await asyncio.wait_for(
            container.auth_context_provider.authenticate(
                request.headers.get("authorization"), request_id, trace_id
            ),
            timeout=container.host_port_timeout_seconds,
        )
    except HostPortError:
        raise
    except (TimeoutError, ConnectionError, OSError) as error:
        raise HostPortError(
            "HOST_PORT_UNAVAILABLE",
            "authentication provider is temporarily unavailable",
            retryable=True,
        ) from error
    if ctx.request_id != request_id or ctx.trace_id != trace_id:
        raise HostPortError("UNAUTHENTICATED", "authentication context binding is invalid")
    return ctx


def require_scope(ctx: RequestContext, scope: str) -> None:
    if scope not in ctx.scopes:
        raise HostPortError("FORBIDDEN", f"{scope} scope is required")
