"""Unit tests for the Phase-3 Kubernetes worker wiring (k8s_worker.py).

Covers the ``AGENT_PLATFORM_WORKER_FACTORY`` contract:

* ``build_attempt_request`` maps a claimed DispatchTicket to a digest-pinned
  AttemptJobRequest (the same shape ``build_attempt_job`` turns into a Job).
* ``K8sJobDispatchRunner.execute`` submits one Job per Attempt through the
  KubernetesOrchestrator with the sandbox namespace + runtime image + the
  Control-Plane Internal API URL injected into the container env.
* The worker factory fails closed without a durable store / pinned image.
"""
from __future__ import annotations

import inspect
import os
from typing import Any

import pytest

from enterprise_agent_platform.domain.records import DispatchTicket
from enterprise_agent_platform.execution.k8s_worker import (
    K8sJobDispatchRunner,
    build_attempt_request,
    create_k8s_worker_scheduler,
    create_worker_store,
    run_worker,
)
from enterprise_agent_platform.execution.orchestrator import KubernetesOrchestrator
from enterprise_agent_platform.persistence import InMemoryPlatformStore

RUNTIME_DIGEST = "sha256:" + "ab" * 32
RUNTIME_IMAGE = f"localhost:5001/enterprise-agent-platform/runtime@{RUNTIME_DIGEST}"


def _ticket(**overrides: Any) -> DispatchTicket:
    fields = {
        "worker_id": "scheduler:test",
        "tenant_id": "demo-tenant",
        "run_id": "run-test",
        "execution_unit_id": "unit-test",
        "attempt_id": "attempt-test",
        "lease_id": "lease-test",
        "generation": 1,
        "source_checkpoint_id": "",
    }
    fields.update(overrides)
    return DispatchTicket(**fields)


class _FakeBatchClient:
    """Records create/delete/list calls without touching a cluster."""

    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[tuple[str, str]] = []

    def create_namespaced_job(self, *, namespace: str, body: dict[str, Any]) -> Any:
        self.created.append((namespace, body))
        return None

    def delete_namespaced_job(
        self, *, namespace: str, name: str, propagation_policy: str
    ) -> Any:
        del propagation_policy
        self.deleted.append((namespace, name))
        return None

    def list_namespaced_pod(self, *, namespace: str, label_selector: str) -> Any:
        del namespace, label_selector
        return type("Pods", (), {"items": []})()


def test_build_attempt_request_maps_ticket_fields() -> None:
    request = build_attempt_request(
        _ticket(generation=3),
        image=RUNTIME_IMAGE,
        control_plane_url="http://control:8080",
        namespace="agent-platform-sandbox",
        service_account="agent-platform-sandbox",
        runtime_class=None,
        active_deadline_seconds=300,
    )
    assert request.tenant_id == "demo-tenant"
    assert request.run_id == "run-test"
    assert request.execution_unit_id == "unit-test"
    assert request.attempt_id == "attempt-test"
    assert request.generation == 3
    assert request.namespace == "agent-platform-sandbox"
    assert request.image == RUNTIME_IMAGE
    assert request.control_plane_url == "http://control:8080"
    assert request.service_account_name == "agent-platform-sandbox"
    assert request.runtime_class_name is None
    assert request.active_deadline_seconds == 300
    assert request.ttl_seconds_after_finished == 600  # Phase 4.5 (4.4 leftover #1)


def test_dispatch_runner_submits_job_with_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBatchClient()
    orchestrator = KubernetesOrchestrator(fake, timeout_seconds=5.0)
    runner = K8sJobDispatchRunner(
        orchestrator,
        image=RUNTIME_IMAGE,
        control_plane_url="http://agent-platform-api.agent-platform-control:8080",
        namespace="agent-platform-sandbox",
        service_account="agent-platform-sandbox",
        runtime_class=None,
        active_deadline_seconds=300,
    )
    ticket = _ticket()

    import asyncio

    asyncio.run(runner.execute(ticket))

    assert len(fake.created) == 1
    namespace, body = fake.created[0]
    assert namespace == "agent-platform-sandbox"
    labels = body["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "enterprise-agent-runtime"
    container = body["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == RUNTIME_IMAGE
    assert container["args"] == [
        "python",
        "-m",
        "enterprise_agent_platform.execution.runtime",
    ]
    env: dict[str, str] = {}
    downrefs = {}
    for item in container["env"]:
        if "value" in item:
            env[item["name"]] = item["value"]
        elif "valueFrom" in item:
            downrefs[item["name"]] = item["valueFrom"]
    assert env["AGENT_PLATFORM_ATTEMPT_ID"] == "attempt-test"
    assert env["AGENT_PLATFORM_GENERATION"] == "1"
    assert env["AGENT_PLATFORM_CONTROL_PLANE_URL"] == (
        "http://agent-platform-api.agent-platform-control:8080"
    )
    assert env["AGENT_PLATFORM_BOOTSTRAP_TOKEN"] == "projected:demo-tenant"
    assert (
        downrefs["AGENT_PLATFORM_POD_UID"]["fieldRef"]["fieldPath"] == "metadata.uid"
    )
    assert body["spec"]["activeDeadlineSeconds"] == 300
    assert body["spec"]["backoffLimit"] == 0
    # Phase 4.5 (4.4 leftover #1): a short Job TTL (600s) so sandbox Jobs are
    # reaped quickly after completion — the 3600s default let 1016 soak runs
    # stack up and exhaust the sandbox quota in the 4.4 pressure test.
    assert body["spec"]["ttlSecondsAfterFinished"] == 600


def test_dispatch_runner_rejects_unpinned_image() -> None:
    with pytest.raises(ValueError):
        build_attempt_request(
            _ticket(),
            image="latest-untagged:1.0",
            control_plane_url="http://control:8080",
            namespace="agent-platform-sandbox",
        )


def test_worker_store_fails_closed_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_PLATFORM_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGENT_PLATFORM_STORE", raising=False)
    with pytest.raises(RuntimeError):
        create_worker_store()


def test_worker_store_memory_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_PLATFORM_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGENT_PLATFORM_STORE", "memory")
    store = create_worker_store()
    assert isinstance(store, InMemoryPlatformStore)


def test_create_k8s_worker_scheduler_wires_dispatch_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_PLATFORM_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGENT_PLATFORM_STORE", "memory")
    monkeypatch.setenv("AGENT_PLATFORM_RUNTIME_IMAGE", RUNTIME_IMAGE)
    monkeypatch.setenv("AGENT_PLATFORM_CONTROL_PLANE_URL", "http://control:8080")
    monkeypatch.setenv("AGENT_PLATFORM_SANDBOX_NAMESPACE", "agent-platform-sandbox")

    fake = _FakeBatchClient()
    k8s = KubernetesOrchestrator(fake, timeout_seconds=5.0)
    scheduler = create_k8s_worker_scheduler(
        store=InMemoryPlatformStore(),
        orchestrator=k8s,
        poll_interval=5.0,
    )
    assert scheduler.poll_interval == 5.0
    assert scheduler._runtime._image == RUNTIME_IMAGE  # type: ignore[attr-defined]
    assert scheduler._runtime._ttl_seconds_after_finished == 600  # type: ignore[attr-defined]

    # One tick against a real queue must produce exactly one submitted Job.
    import asyncio

    from enterprise_agent_platform.contracts.commands import CreateRunCommand
    from enterprise_agent_platform.control.context import RequestContext
    from enterprise_agent_platform.control.service import ControlPlaneService

    control = ControlPlaneService(scheduler.store)
    asyncio.run(
        control.create_run(
            RequestContext(
                tenant_id="demo-tenant",
                actor_id="demo-user",
                scopes=("runs:create",),
                request_id="req-test",
                trace_id="trace-test",
            ),
            CreateRunCommand(
                workflow_type="business-analysis",
                intent="verify k8s dispatch",
                resource_refs=("res://demo/1",),
            ),
            idempotency_key="k8s-worker-test-1",
        )
    )

    async def tick() -> None:
        await scheduler._tick()  # type: ignore[attr-defined]
        await scheduler._tick()  # second tick must find no more work

    asyncio.run(tick())
    assert len(fake.created) == 1
    namespace, body = fake.created[0]
    assert namespace == "agent-platform-sandbox"
    assert body["metadata"]["labels"]["app.kubernetes.io/name"] == (
        "enterprise-agent-runtime"
    )


def test_worker_scheduler_job_ttl_is_env_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 4.5 (4.4 leftover #1): AGENT_PLATFORM_SANDBOX_JOB_TTL_SECONDS
    overrides the 600s default — operators can shrink the TTL under pressure
    without redeploying."""
    monkeypatch.delenv("AGENT_PLATFORM_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGENT_PLATFORM_STORE", "memory")
    monkeypatch.setenv("AGENT_PLATFORM_RUNTIME_IMAGE", RUNTIME_IMAGE)
    monkeypatch.setenv("AGENT_PLATFORM_CONTROL_PLANE_URL", "http://control:8080")
    monkeypatch.setenv("AGENT_PLATFORM_SANDBOX_NAMESPACE", "sandbox")
    monkeypatch.setenv("AGENT_PLATFORM_SANDBOX_JOB_TTL_SECONDS", "120")

    fake = _FakeBatchClient()
    scheduler = create_k8s_worker_scheduler(
        store=InMemoryPlatformStore(),
        orchestrator=KubernetesOrchestrator(fake, timeout_seconds=5.0),
    )
    assert scheduler._runtime._ttl_seconds_after_finished == 120  # type: ignore[attr-defined]


def test_run_worker_returns_awaitable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_PLATFORM_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGENT_PLATFORM_STORE", "memory")
    monkeypatch.setenv("AGENT_PLATFORM_RUNTIME_IMAGE", RUNTIME_IMAGE)
    monkeypatch.setenv("AGENT_PLATFORM_CONTROL_PLANE_URL", "http://control:8080")

    result = run_worker()  # async def: body runs only when awaited
    assert inspect.isawaitable(result)
    result.close()  # never started — safe to discard

    # Unset vars → awaiting the factory must raise (fail closed).
    import asyncio

    monkeypatch.delenv("AGENT_PLATFORM_RUNTIME_IMAGE", raising=False)
    with pytest.raises(RuntimeError):
        asyncio.run(run_worker())


def test_env_example_wiring_target_exists() -> None:
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    target = os.environ.get(
        "AGENT_PLATFORM_WORKER_FACTORY",
        "enterprise_agent_platform.execution.k8s_worker:run_worker",
    )
    module_name, attribute = target.split(":", 1)
    import importlib

    module = importlib.import_module(module_name)
    assert callable(getattr(module, attribute))
    assert callable(module.run_worker)
    assert os.path.exists(os.path.join(root, "deploy", "kind", "values.yaml"))