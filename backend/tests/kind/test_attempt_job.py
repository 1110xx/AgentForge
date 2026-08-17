"""L3 gate: a real sandbox Attempt Job in a disposable Kind cluster.

This test is only meaningful inside a Kind cluster prepared by
``scripts/test-kind.sh`` (Calico CNI, local registry, secrets, migrations).
It is skipped everywhere else:

* ``AGENT_PLATFORM_KIND=1`` must be set by the harness.
* ``AGENT_PLATFORM_KIND_RUNTIME_IMAGE`` pins the digest of the runtime image.

The test drives the real Kubernetes orchestration path: it renders the
Attempt Job spec, submits it through the in-cluster Batch client, waits for
the Pod to reach a terminal phase, then asserts the runtime container exited
successfully. No platform logic runs in-process -- the Attempt executes
inside the Pod, exactly like production.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from enterprise_agent_platform.execution.job_spec import AttemptJobRequest
from enterprise_agent_platform.execution.orchestrator import (
    KubernetesOrchestrator,
    PodObservation,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_PLATFORM_KIND") != "1",
    reason="L3 gate requires a Kind cluster provisioned by scripts/test-kind.sh",
)

RUNTIME_IMAGE = os.environ.get("AGENT_PLATFORM_KIND_RUNTIME_IMAGE", "")
NAMESPACE = "agent-platform-sandbox"
CONTROL_PLANE_URL = "http://agent-platform-api.agent-platform-control:8080"


def _request(attempt_id: str) -> AttemptJobRequest:
    assert RUNTIME_IMAGE, "AGENT_PLATFORM_KIND_RUNTIME_IMAGE is required"
    return AttemptJobRequest(
        tenant_id="kind-e2e",
        run_id=f"run-{uuid.uuid4().hex[:16]}",
        execution_unit_id=f"unit-{uuid.uuid4().hex[:16]}",
        attempt_id=attempt_id,
        generation=1,
        namespace=NAMESPACE,
        image=RUNTIME_IMAGE,
        control_plane_url=CONTROL_PLANE_URL,
        cpu_request="50m",
        cpu_limit="500m",
        memory_request="64Mi",
        memory_limit="256Mi",
        workspace_size="128Mi",
        tmp_size="64Mi",
        active_deadline_seconds=300,
        service_account_name="agent-platform-sandbox",
        runtime_class_name=None,  # Kind does not ship gVisor
        priority_class_name="agent-platform-attempt",
    )


class _KindBatchClient:
    """KubernetesBatchClient adapter bound to the current cluster context."""

    def __init__(self, batch: k8s_client.BatchV1Api, core: k8s_client.CoreV1Api) -> None:
        self._batch = batch
        self._core = core

    def create_namespaced_job(self, *, namespace: str, body: dict[str, object]) -> object:
        return self._batch.create_namespaced_job(namespace=namespace, body=body)

    def delete_namespaced_job(
        self, *, namespace: str, name: str, propagation_policy: str
    ) -> object:
        return self._batch.delete_namespaced_job(
            namespace=namespace, name=name, propagation_policy=propagation_policy
        )

    def list_namespaced_pod(self, *, namespace: str, label_selector: str) -> object:
        return self._core.list_namespaced_pod(namespace=namespace, label_selector=label_selector)


def _load_client() -> tuple[k8s_client.BatchV1Api, k8s_client.CoreV1Api]:
    try:
        k8s_config.load_incluster_config()
    except Exception:  # noqa: BLE001 - runner outside the cluster uses kubeconfig
        k8s_config.load_kube_config()
    return k8s_client.BatchV1Api(), k8s_client.CoreV1Api()


def _pod_exit_code(core: k8s_client.CoreV1Api, namespace: str, job_name: str) -> int | None:
    pods = core.list_namespaced_pod(
        namespace=namespace, label_selector=f"job-name={job_name}"
    )
    items = list(pods.items or [])
    assert items, f"no Pods for Job {job_name}"
    pod = items[0]
    statuses = pod.status.container_statuses or []
    for container in statuses:
        state = container.state
        if state and state.terminated:
            return int(state.terminated.exit_code)
    return None


def test_attempt_job_runs_to_completion_in_kind() -> None:
    assert RUNTIME_IMAGE, "AGENT_PLATFORM_KIND_RUNTIME_IMAGE is required"
    attempt_id = f"kind-e2e-{uuid.uuid4().hex[:12]}"
    batch, core = _load_client()
    orchestrator = KubernetesOrchestrator(_KindBatchClient(batch, core), timeout_seconds=120.0)

    job_name = asyncio.run(orchestrator.submit(_request(attempt_id)))

    try:
        observation: PodObservation | None = None
        for _ in range(150):  # up to 5 minutes at 2s intervals
            observation = asyncio.run(
                orchestrator.observe(namespace=NAMESPACE, job_name=job_name)
            )
            if observation.phase in {"Succeeded", "Failed", "Error"}:
                break
            import time

            time.sleep(2)
        assert observation is not None, "no Pod observed for attempt Job"
        assert observation.phase == "Succeeded", (
            f"attempt Pod did not succeed: phase={observation.phase} reason={observation.reason}"
        )
        assert observation.pod_uid, "attempt Pod has no UID"

        exit_code = _pod_exit_code(core, NAMESPACE, job_name)
        assert exit_code == 0, f"runtime container exited {exit_code}"
    finally:
        asyncio.run(orchestrator.delete(namespace=NAMESPACE, job_name=job_name))
