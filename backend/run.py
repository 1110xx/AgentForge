#!/usr/bin/env python3
"""Demo entry point — wires all adapters for local development.

Now includes the Scheduler background loop that automatically picks up
QUEUED Runs and executes them through the LocalRuntime.

Execution chain (complete):

  API (create_run) → QUEUED
      ↓
  Scheduler (polls list_schedulable_work)
      ↓
  FairScheduler.claim_ready_work() → Attempt + Lease
      ↓
  LocalRuntime.execute()
      ├─ activate_lease() → RUNNING
      ├─ RunSessionProvider.open()
      ├─ RunSessionProvider.run_task() → model calls, events
      ├─ heartbeat / renew_lease()
      └─ complete_run() → SUCCEEDED  (or fail_run() → FAILED)
      ↓
  Events (run.status.changed, attempt.lifecycle, etc.)
  → SSE stream to UI
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import uvicorn

from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.followup import FollowupService
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.execution.scheduler_service import SchedulerService
from enterprise_agent_platform.execution.subprocess_orchestrator import SubprocessOrchestrator
from enterprise_agent_platform.fastapi.app import create_agent_platform_app
from enterprise_agent_platform.fastapi.dependencies import AgentPlatformContainer
from enterprise_agent_platform.integration.host import (
    ResolvedPolicyContext,
    ResolvedResource,
    VerifiedHostContext,
)
from enterprise_agent_platform.persistence import InMemoryPlatformStore
from enterprise_agent_platform.platform.config_reader import ConfigReader
from enterprise_agent_platform.platform.provider_factory import ProviderFactory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Demo protocol adapters (accept everything, full access)
# ---------------------------------------------------------------------------

class DemoAuthProvider:
    """Accepts any Authorization header, returns a full-scope context."""

    async def authenticate(
        self,
        authorization: str | None,
        request_id: str,
        trace_id: str | None,
    ) -> RequestContext:
        return RequestContext(
            tenant_id="demo-tenant",
            actor_id="demo-user",
            scopes=(
                "runs:create",
                "runs:read",
                "runs:write",
                "runs:cancel",
                "runs:act",
                "actions:execute",
                "effects:recover",
                "artifacts:download",
            ),
            request_id=request_id,
            trace_id=trace_id or uuid4().hex,
        )


class DemoResourceResolver:
    async def resolve(
        self,
        ctx: RequestContext,
        resource_ref: str,
    ) -> ResolvedResource:
        return ResolvedResource(
            resource_ref=resource_ref,
            canonical_id=resource_ref,
            tenant_id=ctx.tenant_id,
            owner_id=ctx.actor_id,
            classification="internal",
            version="1",
            digest="sha256:demo",
        )


class DemoHostContextVerifier:
    async def verify(
        self,
        ctx: RequestContext,
        host_context_ref: str,
    ) -> VerifiedHostContext:
        return VerifiedHostContext(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            digest="sha256:host-demo",
            version="1",
        )


class DemoPolicyContextProvider:
    async def resolve(
        self,
        ctx: RequestContext,
        workflow_type: str,
        resources: tuple[ResolvedResource, ...],
        host_context: VerifiedHostContext | None,
    ) -> ResolvedPolicyContext:
        return ResolvedPolicyContext(
            allowed=True,
            policy_version="1",
            policy_digest="sha256:policy-demo",
            scopes=ctx.scopes,
            budget={},
        )


# ---------------------------------------------------------------------------
# Main — wires API, scheduler, and runtime together
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = os.path.join(os.path.dirname(__file__), "config.toml")

    # Read model config, create model provider
    config = ConfigReader(config_path).read()
    factory = ProviderFactory(config)
    run_sessions = factory.create_primary()

    # Store selection: PostgreSQL (SQLAlchemy) when AGENT_PLATFORM_DATABASE_URL is
    # set — the docker-compose stack provides it (postgres:5432); otherwise the
    # in-memory store keeps the local demo portable.
    database_url = os.environ.get("AGENT_PLATFORM_DATABASE_URL", "").strip()
    if database_url:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from enterprise_agent_platform.persistence.sqlalchemy_store import (
            SqlAlchemyPlatformStore,
        )

        _engine = create_async_engine(database_url)
        store = SqlAlchemyPlatformStore(
            async_sessionmaker(_engine, expire_on_commit=False)
        )
        logger.info("using PostgreSQL store (AGENT_PLATFORM_DATABASE_URL)")
    else:
        store = InMemoryPlatformStore()
    control = ControlPlaneService(store)

    # Followup service: terminal Runs are re-scheduled as new Attempts,
    # live Runs answer inline through the model provider session.
    followups = FollowupService(store, control=control, sessions=run_sessions)

    # ── Orchestrator + Scheduler (background loop) ──
    #     Polls QUEUED work, creates Attempt+Lease, then dispatches each
    #     Attempt to its own child runtime process over a JSON-line pipe
    #     (Phase-1: SubprocessOrchestrator; production swaps in KubernetesOrchestrator).
    #     Phase-3 wiring switch: AGENT_PLATFORM_K8S_WORKER=1 runs the scheduler
    #     against the production Kubernetes orchestrator (kind kubeconfig direct
    #     connect) instead of the local subprocess orchestrator.
    orchestrator = SubprocessOrchestrator(
        store=store,
        control=control,
        run_sessions=run_sessions,
        resource_resolver=DemoResourceResolver(),
    )
    if os.environ.get("AGENT_PLATFORM_K8S_WORKER") == "1":
        from enterprise_agent_platform.execution.k8s_worker import (
            K8sJobDispatchRunner,
            make_k8s_batch_client,
        )
        from enterprise_agent_platform.execution.orchestrator import (
            KubernetesOrchestrator,
        )

        orchestrator = K8sJobDispatchRunner(
            KubernetesOrchestrator(make_k8s_batch_client(), timeout_seconds=30.0),
            image=os.environ.get("AGENT_PLATFORM_RUNTIME_IMAGE", ""),
            control_plane_url=os.environ.get(
                "AGENT_PLATFORM_CONTROL_PLANE_URL", "http://127.0.0.1:8080"
            ),
            namespace=os.environ.get(
                "AGENT_PLATFORM_SANDBOX_NAMESPACE", "agent-platform-sandbox"
            ),
            service_account=os.environ.get(
                "AGENT_PLATFORM_SANDBOX_SERVICE_ACCOUNT", "agent-platform-sandbox"
            ),
            runtime_class=os.environ.get("AGENT_PLATFORM_SANDBOX_RUNTIME_CLASS") or None,
        )
        logger.info("Kubernetes orchestrator enabled (AGENT_PLATFORM_K8S_WORKER=1)")
    scheduler = SchedulerService(
        store=store,
        control=control,
        run_sessions=run_sessions,
        orchestrator=orchestrator,
        poll_interval=2.0,
    )

    # Wire everything together
    container = AgentPlatformContainer(
        store=store,
        control=control,
        auth_context_provider=DemoAuthProvider(),
        resource_resolver=DemoResourceResolver(),
        host_context_verifier=DemoHostContextVerifier(),
        policy_context_provider=DemoPolicyContextProvider(),
        run_sessions=run_sessions,
        followups=followups,
    )

    app = create_agent_platform_app(container)

    # ── Run API server + scheduler loop concurrently ──
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=8080,
        log_level="info",
    )
    server = uvicorn.Server(config)

    async def startup() -> None:
        """Start scheduler loop and server."""
        scheduler_task = asyncio.create_task(scheduler.run_loop())
        logger.info("Scheduler background loop started")

        # Try to register signal handlers for graceful shutdown.
        # On Windows, add_signal_handler may raise NotImplementedError.
        loop = asyncio.get_running_loop()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(
                    sig,
                    lambda: asyncio.create_task(_shutdown(server, scheduler_task)),
                )
        except NotImplementedError:
            logger.info("Signal handlers not supported on this platform; Ctrl+C to quit.")

        await server.serve()
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass

    async def _shutdown(
        server: uvicorn.Server,
        scheduler_task: asyncio.Task,
    ) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down...")
        scheduler_task.cancel()
        server.should_exit = True

    try:
        asyncio.run(startup())
    except KeyboardInterrupt:
        logger.info("Interrupted, exiting.")


if __name__ == "__main__":
    main()