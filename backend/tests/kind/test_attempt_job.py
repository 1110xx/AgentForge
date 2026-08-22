"""L3 gate: a real sandbox Attempt Job in a disposable Kind cluster.

This test is only meaningful inside a Kind cluster prepared by
``scripts/test-kind.sh`` (Calico CNI, local registry, shared PostgreSQL,
K8s worker factory wired). It is skipped everywhere else:

* ``AGENT_PLATFORM_KIND=1`` must be set by the harness.
* ``AGENT_PLATFORM_KIND_API_URL`` is the Control-Plane API reachable from the
  test host (the harness runs ``kubectl port-forward`` on svc/agent-platform-api).
* ``AGENT_PLATFORM_KIND_RUNTIME_IMAGE`` pins the digest of the runtime image.

The test drives the **full production path** — nothing runs in-process except
the HTTP client:

1. POST /v1/runs on the deployed API → Run QUEUED (durable Postgres).
2. The deployed K8s worker (`agent-platform-orchestrator`, scheduler + Job
   dispatch) claims the attempt and submits the sandbox Job.
3. The Pod's runtime bootstraps against the same API, renews its lease, runs
   the model call, commits turn/final checkpoints, and self-reports the
   terminal state — ending with the Run SUCCEEDED + a Succeeded Pod.

Assertions cover both the Control-Plane record (status + events) and the
Kubernetes object side (Pod phase).
"""
from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_PLATFORM_KIND") != "1",
    reason="L3 gate requires a Kind cluster provisioned by scripts/test-kind.sh",
)

KIND_API_URL = os.environ.get("AGENT_PLATFORM_KIND_API_URL", "").strip()
RUNTIME_IMAGE = os.environ.get("AGENT_PLATFORM_KIND_RUNTIME_IMAGE", "")
TENANT = "kind-e2e"
BEARER = "Bearer reference-local-demo"
NAMESPACE = "agent-platform-sandbox"
JOB_LABEL = "app.kubernetes.io/name=enterprise-agent-runtime"


def _k8s_core() -> k8s_client.CoreV1Api:
    try:
        k8s_config.load_incluster_config()
    except Exception:  # noqa: BLE001 - runner outside the cluster uses kubeconfig
        k8s_config.load_kube_config()
    return k8s_client.CoreV1Api()


def _create_run(client: httpx.Client) -> str:
    response = client.post(
        "/v1/runs",
        headers={
            "Authorization": BEARER,
            "Idempotency-Key": f"kind-e2e-{uuid.uuid4().hex[:16]}",
        },
        json={
            "workflow_type": "synthetic-analysis",
            "intent": "Analyze a portable synthetic resource",
            "resource_refs": ["synthetic-case:case-42"],
            "parameters": {"analysis_mode": "summary", "max_items": 10},
            "host_context_ref": "reference-context:demo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


def _wait_run_terminal(client: httpx.Client, run_id: str, timeout_s: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/runs/{run_id}", headers={"Authorization": BEARER})
        assert response.status_code == 200, response.text
        last = response.json()
        status = last["status"]
        if status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return last
        time.sleep(2.0)
    raise AssertionError(
        f"Run {run_id} did not reach a terminal state within {timeout_s}s; "
        f"last status={last.get('status')} body={last}"
    )


def test_attempt_job_runs_to_completion_in_kind() -> None:
    assert RUNTIME_IMAGE, "AGENT_PLATFORM_KIND_RUNTIME_IMAGE is required"
    assert KIND_API_URL, "AGENT_PLATFORM_KIND_API_URL is required (port-forward)"
    core = _k8s_core()

    with httpx.Client(base_url=KIND_API_URL, timeout=30.0) as client:
        run_id = _create_run(client)
        final = _wait_run_terminal(client, run_id)
        assert final["status"] == "SUCCEEDED", final

        events = client.get(
            f"/v1/runs/{run_id}/events", headers={"Authorization": BEARER}
        ).json()
        event_types = [event["event_type"] for event in events["events"]]
        assert "attempt.lifecycle" in event_types, event_types
        assert "run.status.changed" in event_types, event_types
        assert any(
            "SUCCEEDED" in str(event.get("details", {}))
            for event in events["events"]
        ), "no SUCCEEDED detail found in events"

    # Kubernetes object side: the sandbox Pod must have Succeeded and exited 0.
    pods = core.list_namespaced_pod(namespace=NAMESPACE, label_selector=JOB_LABEL)
    items = list(pods.items or [])
    assert items, f"no sandbox Pods found (label {JOB_LABEL})"
    pod = items[0]
    assert pod.status.phase == "Succeeded", (
        f"attempt Pod did not succeed: phase={pod.status.phase} "
        f"reason={pod.status.reason}"
    )
    assert pod.metadata.uid, "attempt Pod has no UID"
    for container in pod.status.container_statuses or []:
        if container.name == "agent-runtime":
            assert container.state.terminated is not None, "runtime did not terminate"
            assert container.state.terminated.exit_code == 0, (
                f"runtime exited {container.state.terminated.exit_code}"
            )


def test_kind_api_requires_reference_token() -> None:
    """The deployed API must reject unauthenticated public requests."""
    assert KIND_API_URL, "AGENT_PLATFORM_KIND_API_URL is required (port-forward)"
    with httpx.Client(base_url=KIND_API_URL, timeout=30.0) as client:
        # A missing Authorization header must surface as an authz error
        # (before any route-level 404/405); Run lookup is the most direct probe.
        response = client.get("/v1/runs/run-does-not-exist")
        assert response.status_code in {401, 403}, response.text