"""Async adapter around a synchronous Kubernetes Batch client."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from .job_spec import AttemptJobRequest, build_attempt_job


class KubernetesBatchClient(Protocol):
    def create_namespaced_job(self, *, namespace: str, body: dict[str, Any]) -> Any: ...
    def delete_namespaced_job(
        self, *, namespace: str, name: str, propagation_policy: str
    ) -> Any: ...
    def list_namespaced_pod(self, *, namespace: str, label_selector: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class PodObservation:
    job_name: str
    pod_uid: str | None
    phase: str
    reason: str | None
    observed_at_resource_version: str | None


class KubernetesOrchestrator:
    def __init__(self, client: KubernetesBatchClient, *, timeout_seconds: float = 30.0) -> None:
        self._client = client
        self._timeout = timeout_seconds

    async def submit(self, request: AttemptJobRequest) -> str:
        body = build_attempt_job(request)
        await asyncio.wait_for(
            asyncio.to_thread(
                self._client.create_namespaced_job,
                namespace=request.namespace,
                body=body,
            ),
            timeout=self._timeout,
        )
        return str(body["metadata"]["name"])

    async def delete(self, *, namespace: str, job_name: str) -> None:
        await asyncio.wait_for(
            asyncio.to_thread(
                self._client.delete_namespaced_job,
                namespace=namespace,
                name=job_name,
                propagation_policy="Foreground",
            ),
            timeout=self._timeout,
        )

    async def observe(self, *, namespace: str, job_name: str) -> PodObservation:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                self._client.list_namespaced_pod,
                namespace=namespace,
                label_selector=f"job-name={job_name}",
            ),
            timeout=self._timeout,
        )
        items = list(getattr(response, "items", []))
        if not items:
            return PodObservation(job_name, None, "PENDING", "POD_NOT_OBSERVED", None)
        pod = items[0]
        return PodObservation(
            job_name=job_name,
            pod_uid=getattr(getattr(pod, "metadata", None), "uid", None),
            phase=str(getattr(getattr(pod, "status", None), "phase", "UNKNOWN")),
            reason=getattr(getattr(pod, "status", None), "reason", None),
            observed_at_resource_version=getattr(
                getattr(pod, "metadata", None), "resource_version", None
            ),
        )
