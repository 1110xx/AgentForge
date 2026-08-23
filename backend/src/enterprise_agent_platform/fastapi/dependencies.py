"""Request-scoped trusted context resolution for the mountable FastAPI router."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from fastapi import Request

from enterprise_agent_platform.artifacts.downloads import ArtifactDownloadRequest
from enterprise_agent_platform.contracts.commands import FollowupCommand, UiActionCommand
from enterprise_agent_platform.contracts.models import (
    ArtifactDownloadAuthorization,
    FollowupAnswer,
    FollowupHistoryPage,
)
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.execution.session import RunSessionProvider
from enterprise_agent_platform.integration.host import (
    AuthContextProvider,
    HostContextVerifier,
    HostPortError,
    PolicyContextProvider,
    ResourceResolver,
)
from enterprise_agent_platform.persistence.protocol import PlatformStore
from enterprise_agent_platform.platform.config_reader import AppConfig, ConfigReader
from enterprise_agent_platform.platform.provider_factory import ProviderFactory
from enterprise_agent_platform.platform.telemetry import DiagnosticTelemetry

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


class FollowupHandler(Protocol):
    async def followup(
        self,
        ctx: RequestContext,
        run_id: str,
        command: FollowupCommand,
        idempotency_key: str,
    ) -> FollowupAnswer: ...

    async def list_followups(
        self,
        ctx: RequestContext,
        run_id: str,
    ) -> FollowupHistoryPage: ...


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
    run_sessions: RunSessionProvider | None = None
    followups: FollowupHandler | None = None
    telemetry: DiagnosticTelemetry | None = None

    def __post_init__(self) -> None:
        if self.host_port_timeout_seconds <= 0:
            raise ValueError("host port timeout must be positive")
        if self.sse_heartbeat_seconds <= 0 or self.sse_max_lifetime_seconds <= 0:
            raise ValueError("SSE timing values must be positive")
        if not 1 <= self.sse_batch_size <= 100:
            raise ValueError("SSE batch size must be between 1 and 100")

    @classmethod
    def from_config(cls, config_path: str | None = None) -> "AgentPlatformContainer":
        """Create a container with model provider from config.toml.

        用户只需编辑 ``config.toml`` 即可切换提供商、设置 API Key、调整参数。
        无需修改任何代码。
        """
        from enterprise_agent_platform.control.followup import FollowupService
        from enterprise_agent_platform.persistence import InMemoryPlatformStore

        reader = ConfigReader(config_path)
        app_config = reader.read()

        factory = ProviderFactory(app_config)
        run_sessions = factory.create_primary()

        store = InMemoryPlatformStore()
        control = ControlPlaneService(store)
        followups = FollowupService(store, control=control, sessions=run_sessions)

        return cls(
            store=store,
            control=control,
            auth_context_provider=None,  # Must be injected by host
            resource_resolver=None,       # Must be injected by host
            host_context_verifier=None,    # Must be injected by host
            policy_context_provider=None,  # Must be injected by host
            run_sessions=run_sessions,
            followups=followups,
        )

    def print_config_status(self) -> None:
        """Print current configuration status for user debugging."""
        if self.run_sessions:
            try:
                reader = ConfigReader()
                provider_factory.print_config_status(reader.read())
            except Exception:
                print("⚠️  Unable to read config.toml for status display")

    @classmethod
    def quick_start(cls) -> "AgentPlatformContainer":
        """Quick start: read config.toml, create all components, print status."""
        container = cls.from_config()
        print("✅ Agent Platform container created from config.toml")
        if container.run_sessions:
            resolved = container.run_sessions.__class__.__name__
            print(f"   Model provider: {resolved}")
        print()
        container.print_config_status()
        return container


def request_trace_id(request: Request) -> str:
    existing = getattr(request.state, "trace_id", None)
    if isinstance(existing, str) and existing:
        return existing
    match = TRACEPARENT.fullmatch(request.headers.get("traceparent", ""))
    trace_id = match.group(1).lower() if match else uuid4().hex
    request.state.trace_id = trace_id
    return trace_id


def create_default_container() -> AgentPlatformContainer:
    """Create a default container with configured model provider."""
    from enterprise_agent_platform.config import PlatformSettings
    
    settings = PlatformSettings()
    run_sessions = create_model_provider(settings)
    
    return AgentPlatformContainer(
        store=PlatformStore(),  # This would be properly configured in production
        control=ControlPlaneService(),
        auth_context_provider=AuthContextProvider(),
        resource_resolver=ResourceResolver(),
        host_context_verifier=HostContextVerifier(),
        policy_context_provider=PolicyContextProvider(),
        run_sessions=run_sessions,
        followups=_create_default_followup_handler(run_sessions)
    )


class _DefaultFollowupHandler:
    """Simple in-memory followup handler used when no FollowupService is configured."""

    def __init__(self, run_sessions: RunSessionProvider) -> None:
        self._run_sessions = run_sessions
        self._answers: dict[tuple[str, str], FollowupAnswer] = {}
        self._records: dict[str, list[FollowupRecord]] = {}
        self._next_seq: dict[str, int] = {}

    async def followup(
        self,
        ctx: RequestContext,
        run_id: str,
        command: FollowupCommand,
        idempotency_key: str,
    ) -> FollowupAnswer:
        from enterprise_agent_platform.contracts.models import FollowupAnswer

        cached = self._answers.get((run_id, idempotency_key))
        if cached is not None:
            return cached

        import datetime
        now = datetime.datetime.now(datetime.UTC)
        session_id = ""
        try:
            handle = await self._run_sessions.open(
                run_id=run_id,
                intent="Follow-up question",
                resource_refs=(),
                host_context_ref=None,
            )
            session_id = handle.session_id
            answer = await self._run_sessions.followup(
                handle=handle,
                message=command.question,
                read_only=True,
            )
            await self._run_sessions.close(handle)
        except Exception as e:
            answer = f"Error processing follow-up: {e}"

        result = FollowupAnswer(
            schema_version="followup-answer/v1",
            run_id=run_id,
            session_id=session_id,
            question=command.question,
            answer=answer,
        )
        self._answers[(run_id, idempotency_key)] = result

        seq = self._next_seq.setdefault(run_id, 0)
        self._records.setdefault(run_id, []).append(
            FollowupRecord(
                schema_version="followup-record/v1",
                run_id=run_id,
                followup_seq=seq,
                question=command.question,
                answer=answer,
                answered_at=now,
                client_followup_id=command.client_followup_id,
            )
        )
        self._next_seq[run_id] = seq + 1

        return result

    async def list_followups(
        self,
        ctx: RequestContext,
        run_id: str,
    ) -> FollowupHistoryPage:
        records = tuple(self._records.get(run_id, []))
        return FollowupHistoryPage(
            schema_version="followup-history-page/v1",
            run_id=run_id,
            total_count=len(records),
            records=records,
        )


def _create_default_followup_handler(run_sessions: RunSessionProvider) -> FollowupHandler:
    """Create a default followup handler that satisfies the FollowupHandler protocol."""
    return _DefaultFollowupHandler(run_sessions)


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
