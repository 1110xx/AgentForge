"""Kubernetes worker factory + Job dispatch runner (Phase 3 wiring).

Serves as the ``AGENT_PLATFORM_WORKER_FACTORY`` target (contract:
``module:callable`` returning an awaitable, see ``platform/entrypoint.py``).

The worker connects the background ``SchedulerService`` to the production
``KubernetesOrchestrator``: every claimed Attempt becomes one K8s Job/Pod
(build_attempt_job). The Pod is a **self-reporting Runner** — it bootstraps,
restores checkpoints, renews its lease, and commits the terminal checkpoint
through the Control-Plane Internal API (``http-runtime`` transport), so the
worker only needs to submit the Job and let the Pod drive itself to a terminal
state. No in-process completer is required.

Env contract (aligned with ``deploy/helm/templates/workers.yaml``):

* ``AGENT_PLATFORM_WORKER_FACTORY``        — ``module:callable`` (usually
  ``enterprise_agent_platform.execution.k8s_worker:run_worker``)
* ``AGENT_PLATFORM_DATABASE_URL``          — durable store (required in prod;
  service fails closed when absent unless ``AGENT_PLATFORM_STORE=memory``)
* ``AGENT_PLATFORM_RUNTIME_IMAGE``         — runtime image pinned by sha256 digest
* ``AGENT_PLATFORM_CONTROL_PLANE_URL``     — Internal API base URL the Pod calls
* ``AGENT_PLATFORM_SANDBOX_NAMESPACE``     — namespace for Attempt Jobs
* ``AGENT_PLATFORM_SANDBOX_SERVICE_ACCOUNT`` — Job service account
* ``AGENT_PLATFORM_SANDBOX_RUNTIME_CLASS`` — optional runtime class (empty = none)
* ``AGENT_PLATFORM_SANDBOX_HOST_NETWORK`` — 1/true: attempt Jobs run with
  ``hostNetwork: true`` + ``dnsPolicy: ClusterFirstWithHostNet`` (egress
  workaround for hosts whose Pod overlay cannot reach external/node
  endpoints; default off)
* ``AGENT_PLATFORM_WORKER_POLL_INTERVAL``  — scheduler poll (default 2.0s)
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.domain.records import DispatchTicket
from enterprise_agent_platform.execution.job_spec import AttemptJobRequest
from enterprise_agent_platform.execution.orchestrator import KubernetesOrchestrator
from enterprise_agent_platform.execution.scheduler_service import SchedulerService
from enterprise_agent_platform.persistence.protocol import PlatformStore
from enterprise_agent_platform.platform.relay import create_relay_from_env
from enterprise_agent_platform.platform.telemetry import DiagnosticTelemetry
from enterprise_agent_platform.platform.telemetry_service import create_telemetry_from_env

logger = logging.getLogger(__name__)

IMAGE_DIGEST_SUFFIX = "@sha256:"


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _env(name: str, *, required: bool = False, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise RuntimeError(f"{name} is required for the Kubernetes worker")
    return value or default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    return default


# ---------------------------------------------------------------------------
# Kubernetes API client (out-of-cluster kubeconfig / in-cluster)
# ---------------------------------------------------------------------------


class KubernetesClientAdapter:
    """Combine BatchV1Api + CoreV1Api into the ``KubernetesBatchClient`` Protocol."""

    def __init__(self, batch: Any, core: Any) -> None:
        self._batch = batch
        self._core = core

    def create_namespaced_job(self, *, namespace: str, body: dict[str, Any]) -> Any:
        return self._batch.create_namespaced_job(namespace=namespace, body=body)

    def delete_namespaced_job(
        self, *, namespace: str, name: str, propagation_policy: str
    ) -> Any:
        return self._batch.delete_namespaced_job(
            namespace=namespace, name=name, propagation_policy=propagation_policy
        )

    def list_namespaced_pod(self, *, namespace: str, label_selector: str) -> Any:
        return self._core.list_namespaced_pod(namespace=namespace, label_selector=label_selector)


def make_k8s_batch_client() -> KubernetesClientAdapter:
    """Build a cluster-bound Batch client (in-cluster config preferred)."""
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
    except Exception:  # noqa: BLE001 - runner outside the cluster uses kubeconfig
        k8s_config.load_kube_config()
    return KubernetesClientAdapter(
        batch=k8s_client.BatchV1Api(),
        core=k8s_client.CoreV1Api(),
    )


# ---------------------------------------------------------------------------
# DispatchTicket → AttemptJobRequest mapping
# ---------------------------------------------------------------------------


def build_attempt_request(
    ticket: DispatchTicket,
    *,
    image: str,
    control_plane_url: str,
    namespace: str,
    service_account: str = "agent-platform-sandbox",
    runtime_class: str | None = None,
    active_deadline_seconds: int = 300,
    ttl_seconds_after_finished: int = 600,
    cpu_request: str = "50m",
    cpu_limit: str = "500m",
    memory_request: str = "64Mi",
    memory_limit: str = "256Mi",
    workspace_size: str = "128Mi",
    tmp_size: str = "64Mi",
    bootstrap_token: str | None = None,
    extra_env: tuple[tuple[str, str], ...] = (),
    host_network: bool = False,
    dns_policy: str | None = None,
) -> AttemptJobRequest:
    """Map a claimed DispatchTicket to the K8s Attempt Job spec.

    The image must already be pinned by sha256 digest (fail early here, and
    ``build_attempt_job`` re-validates it); ``control_plane_url`` is what the
    Pod's runtime uses to reach the Control-Plane Internal API.
    """
    import re

    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image):
        raise ValueError("runtime image must be pinned by sha256 digest")
    return AttemptJobRequest(
        tenant_id=ticket.tenant_id,
        run_id=ticket.run_id,
        execution_unit_id=ticket.execution_unit_id,
        attempt_id=ticket.attempt_id,
        generation=ticket.generation,
        namespace=namespace,
        image=image,
        control_plane_url=control_plane_url,
        cpu_request=cpu_request,
        cpu_limit=cpu_limit,
        memory_request=memory_request,
        memory_limit=memory_limit,
        workspace_size=workspace_size,
        tmp_size=tmp_size,
        active_deadline_seconds=active_deadline_seconds,
        ttl_seconds_after_finished=ttl_seconds_after_finished,
        service_account_name=service_account,
        runtime_class_name=runtime_class,
        bootstrap_token=bootstrap_token,
        extra_env=extra_env,
        host_network=host_network,
        dns_policy=dns_policy,
    )


class K8sJobDispatchRunner:
    """Scheduler orchestrator adapter: submit one K8s Job per Attempt.

    ``execute(ticket)`` matches the orchestrator hook the SchedulerService
    dispatches to (``LocalRuntime`` / ``SubprocessOrchestrator`` use the same
    signature). It submits the Job and returns — the Pod runtime self-reports
    its terminal state through the Internal API (bootstrap → restore →
    heartbeat → commit_final / record_failure), so no parent-side completer
    runs here. ``active_deadline_seconds`` on the Job bounds runaway attempts.
    """

    def __init__(
        self,
        orchestrator: KubernetesOrchestrator,
        *,
        image: str,
        control_plane_url: str,
        namespace: str,
        service_account: str = "agent-platform-sandbox",
        runtime_class: str | None = None,
        active_deadline_seconds: int = 300,
        ttl_seconds_after_finished: int = 600,
        bootstrap_token: str | None = None,
        telemetry: DiagnosticTelemetry | None = None,
        host_network: bool = False,
        dns_policy: str | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._image = image
        self._control_plane_url = control_plane_url
        self._namespace = namespace
        self._service_account = service_account
        self._runtime_class = runtime_class
        self._active_deadline_seconds = active_deadline_seconds
        self._ttl_seconds_after_finished = ttl_seconds_after_finished
        self._bootstrap_token = bootstrap_token
        self._telemetry = telemetry
        self._host_network = host_network
        self._dns_policy = dns_policy

    async def execute(self, ticket: DispatchTicket) -> None:
        import time as _time

        # Phase 4.3: inherit the observability env contract so runner Pods
        # export OTLP spans/metrics + JSON logs into the same stack.
        extra_env = tuple(
            (key, value)
            for key, value in (
                ("AGENT_PLATFORM_OTLP_ENDPOINT", _env("AGENT_PLATFORM_OTLP_ENDPOINT")),
                ("AGENT_PLATFORM_JSON_LOGS", _env("AGENT_PLATFORM_JSON_LOGS")),
                (
                    "AGENT_PLATFORM_PROMETHEUS_ENABLED",
                    _env("AGENT_PLATFORM_PROMETHEUS_ENABLED"),
                ),
            )
            if value
        )
        request = build_attempt_request(
            ticket,
            image=self._image,
            control_plane_url=self._control_plane_url,
            namespace=self._namespace,
            service_account=self._service_account,
            runtime_class=self._runtime_class,
            active_deadline_seconds=self._active_deadline_seconds,
            ttl_seconds_after_finished=self._ttl_seconds_after_finished,
            bootstrap_token=self._bootstrap_token
            or f"projected:{ticket.tenant_id}",
            extra_env=extra_env,
            host_network=self._host_network,
            dns_policy=self._dns_policy,
        )
        started = _time.perf_counter()
        tele = self._telemetry
        job_name = await self._orchestrator.submit(request)
        if tele is not None:
            trace = tele.begin_trace()
            try:
                with tele.span(
                    "job.submit",
                    attributes={
                        "agent.platform.run.id": ticket.run_id,
                        "agent.platform.attempt.id": ticket.attempt_id,
                        "agent.platform.lease.generation": ticket.generation,
                    },
                    trace=trace,
                ):
                    pass
            except Exception:  # noqa: BLE001, S110 - diagnostics never gate dispatch
                pass
            try:
                tele.timing(
                    "agent_platform_job_submit_seconds",
                    _time.perf_counter() - started,
                    labels={"operation": "job.submit"},
                )
            except Exception:  # noqa: BLE001, S110 - diagnostics never gate dispatch
                pass
        logger.info(
            "Submitted Attempt Job: run=%s attempt=%s generation=%d job=%s ns=%s",
            ticket.run_id,
            ticket.attempt_id,
            ticket.generation,
            job_name,
            self._namespace,
        )


# ---------------------------------------------------------------------------
# Store bootstrap (fail closed unless explicitly memory)
# ---------------------------------------------------------------------------


def create_worker_store() -> PlatformStore:
    """Build the durable store the worker shares with the Control-Plane API.

    Production uses PostgreSQL (``AGENT_PLATFORM_DATABASE_URL``). For local
    Kind/demo first-wiring runs ``AGENT_PLATFORM_STORE=memory`` opts into the
    in-memory store; anything else without a database URL fails closed.
    """
    database_url = os.environ.get("AGENT_PLATFORM_DATABASE_URL", "").strip()
    if database_url:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from enterprise_agent_platform.persistence.sqlalchemy_store import (
            SqlAlchemyPlatformStore,
        )

        _engine = create_async_engine(database_url)
        return SqlAlchemyPlatformStore(async_sessionmaker(_engine, expire_on_commit=False))

    if os.environ.get("AGENT_PLATFORM_STORE", "").strip() == "memory":
        from enterprise_agent_platform.persistence import InMemoryPlatformStore

        return InMemoryPlatformStore()

    raise RuntimeError(
        "AGENT_PLATFORM_DATABASE_URL is required for the Kubernetes worker "
        "(set AGENT_PLATFORM_STORE=memory only for local demo runs)"
    )


# ---------------------------------------------------------------------------
# Worker assembly (testable) + entrypoint factory (awaitable)
# ---------------------------------------------------------------------------


def create_k8s_worker_scheduler(
    *,
    store: PlatformStore | None = None,
    orchestrator: KubernetesOrchestrator | None = None,
    poll_interval: float | None = None,
) -> SchedulerService:
    """Assemble the K8s worker: store + control + Job dispatch runner."""
    image = _env("AGENT_PLATFORM_RUNTIME_IMAGE", required=True)
    if IMAGE_DIGEST_SUFFIX not in image:
        raise RuntimeError(
            "AGENT_PLATFORM_RUNTIME_IMAGE must be pinned by sha256 digest "
            "(host:5001/repo:tag@sha256:<64 hex>)"
        )
    control_plane_url = _env("AGENT_PLATFORM_CONTROL_PLANE_URL", required=True)
    namespace = _env("AGENT_PLATFORM_SANDBOX_NAMESPACE", default="agent-platform-sandbox")
    service_account = _env(
        "AGENT_PLATFORM_SANDBOX_SERVICE_ACCOUNT", default="agent-platform-sandbox"
    )
    runtime_class_raw = _env("AGENT_PLATFORM_SANDBOX_RUNTIME_CLASS")
    runtime_class = runtime_class_raw or None

    store = store or create_worker_store()
    telemetry = create_telemetry_from_env(
        service_name="enterprise-agent-platform-worker"
    )
    control = ControlPlaneService(store, telemetry=telemetry)
    k8s = orchestrator or KubernetesOrchestrator(
        make_k8s_batch_client(),
        timeout_seconds=_env_float("AGENT_PLATFORM_K8S_API_TIMEOUT", 30.0),
    )
    runner = K8sJobDispatchRunner(
        k8s,
        image=image,
        control_plane_url=control_plane_url,
        namespace=namespace,
        service_account=service_account,
        runtime_class=runtime_class,
        active_deadline_seconds=_env_int(
            "AGENT_PLATFORM_SANDBOX_ATTEMPT_DEADLINE_SECONDS", 300
        ),
        ttl_seconds_after_finished=_env_int(
            "AGENT_PLATFORM_SANDBOX_JOB_TTL_SECONDS", 600
        ),
        host_network=_env_bool("AGENT_PLATFORM_SANDBOX_HOST_NETWORK"),
        dns_policy=(
            "ClusterFirstWithHostNet"
            if _env_bool("AGENT_PLATFORM_SANDBOX_HOST_NETWORK")
            else None
        ),
        telemetry=telemetry,
    )
    return SchedulerService(
        store=store,
        control=control,
        orchestrator=runner,
        poll_interval=poll_interval
        if poll_interval is not None
        else _env_float("AGENT_PLATFORM_WORKER_POLL_INTERVAL", 2.0),
        telemetry=telemetry,
    )


async def run_worker() -> None:
    """Entry factory for ``AGENT_PLATFORM_WORKER_FACTORY`` (must be awaitable).

    Runs the scheduler loop plus the NATS outbox relay + wake-up consumer
    (when ``AGENT_PLATFORM_NATS_URL`` is configured) until cancelled by the
    process supervisor (asyncio.run cancels the main task on SIGINT/SIGTERM).
    """
    scheduler = create_k8s_worker_scheduler()
    logger.info(
        "Kubernetes worker started (worker=%s, poll_interval=%.1fs)",
        scheduler.worker_id,
        scheduler.poll_interval,
    )
    services = create_relay_from_env(scheduler.store, wake=scheduler.wake)
    tasks = [asyncio.create_task(scheduler.run_loop())]
    if services is not None:
        tasks.append(asyncio.create_task(services.relay.run_loop()))
        tasks.append(asyncio.create_task(services.consumer.consume_loop()))
        logger.info("NATS relay + wake-up consumer enabled")
    else:
        logger.info("NATS relay disabled (AGENT_PLATFORM_NATS_URL not set)")
    try:
        await asyncio.gather(*tasks)
    finally:
        if services is not None:
            await services.bus.close()


__all__ = [
    "K8sJobDispatchRunner",
    "KubernetesClientAdapter",
    "build_attempt_request",
    "create_k8s_worker_scheduler",
    "create_worker_store",
    "make_k8s_batch_client",
    "run_worker",
]