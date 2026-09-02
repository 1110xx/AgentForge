from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

_DIGEST_IMAGE = re.compile(r".+@sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class AttemptJobRequest:
    tenant_id: str
    run_id: str
    execution_unit_id: str
    attempt_id: str
    generation: int
    namespace: str
    image: str
    control_plane_url: str
    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str
    workspace_size: str
    tmp_size: str
    active_deadline_seconds: int
    # Phase 4.5 (4.4 leftover #1): completed Jobs are reaped by the K8s
    # TTL-controller after ``ttl_seconds_after_finished``. The historical 3600s
    # default let finished Jobs pile up against the sandbox quota during
    # high-throughput bursts (observed in Phase 4.4: 1016 runs filled the
    # 100-Job count quota and stalled scheduling). Short default (600s) keeps
    # the quota headroom without relying on an external cleanup CronJob.
    ttl_seconds_after_finished: int = 600
    service_account_name: str = "agent-platform-sandbox"
    runtime_class_name: str | None = "agent-platform-gvisor"
    priority_class_name: str = "agent-platform-attempt"
    # Demo bootstrap identity: ``projected:{tenant_id}`` is accepted by the
    # Internal API bootstrap (fastapi/internal_adapter.py). Production swaps
    # real K8s service-account projection validation; when unset the Pod falls
    # back to reading the projected SA token volume.
    bootstrap_token: str | None = None
    # Phase 4.3 (G5): extra env passthrough so runner Pods inherit the
    # observability contract (AGENT_PLATFORM_OTLP_ENDPOINT / JSON_LOGS /
    # PROMETHEUS_ENABLED) from the worker, turning the runtime's per-attempt
    # spans into the same trace + log stream the control plane exports.
    extra_env: tuple[tuple[str, str], ...] = ()


def _job_name(attempt_id: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", attempt_id.lower()).strip("-")
    normalized = normalized[:36].rstrip("-") or "attempt"
    suffix = hashlib.sha256(attempt_id.encode()).hexdigest()[:10]
    return f"agent-attempt-{normalized}-{suffix}"


def build_attempt_job(request: AttemptJobRequest) -> dict[str, Any]:
    if request.generation < 1:
        raise ValueError("generation must be positive")
    if not _DIGEST_IMAGE.fullmatch(request.image):
        raise ValueError("runtime image must be pinned by sha256 digest")
    if request.active_deadline_seconds < 1:
        raise ValueError("active deadline must be positive")

    name = _job_name(request.attempt_id)
    labels = {
        "app.kubernetes.io/name": "enterprise-agent-runtime",
        "agent.platform/attempt-hash": hashlib.sha256(request.attempt_id.encode()).hexdigest()[:16],
        "agent.platform/network-profile": "runtime-no-kube-api",
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": request.namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": request.active_deadline_seconds,
            "ttlSecondsAfterFinished": request.ttl_seconds_after_finished,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": request.service_account_name,
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "enableServiceLinks": False,
                    **(
                        {"runtimeClassName": request.runtime_class_name}
                        if request.runtime_class_name
                        else {}
                    ),
                    "priorityClassName": request.priority_class_name,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "fsGroup": 65532,
                        "fsGroupChangePolicy": "OnRootMismatch",
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "agent-runtime",
                            "image": request.image,
                            "imagePullPolicy": "IfNotPresent",
                            "workingDir": "/workspace",
                            "args": ["python", "-m", "enterprise_agent_platform.execution.runtime"],
                            "env": [
                                {"name": "AGENT_PLATFORM_ATTEMPT_ID", "value": request.attempt_id},
                                {"name": "AGENT_PLATFORM_GENERATION", "value": str(request.generation)},
                                {
                                    "name": "AGENT_PLATFORM_CONTROL_PLANE_URL",
                                    "value": request.control_plane_url,
                                },
                                {
                                    "name": "AGENT_PLATFORM_POD_UID",
                                    "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                                },
                                *(
                                    [
                                        {
                                            "name": "AGENT_PLATFORM_BOOTSTRAP_TOKEN",
                                            "value": request.bootstrap_token,
                                        }
                                    ]
                                    if request.bootstrap_token
                                    else []
                                ),
                                *(
                                    [{"name": key, "value": value} for key, value in request.extra_env]
                                ),
                            ],
                            "resources": {
                                "requests": {
                                    "cpu": request.cpu_request,
                                    "memory": request.memory_request,
                                },
                                "limits": {
                                    "cpu": request.cpu_limit,
                                    "memory": request.memory_limit,
                                },
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True,
                                "runAsUser": 65532,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "volumeMounts": [
                                {"name": "inputs", "mountPath": "/inputs", "readOnly": True},
                                {"name": "workspace", "mountPath": "/workspace"},
                                {"name": "runtime-tmp", "mountPath": "/tmp"},
                                {
                                    "name": "runtime-identity",
                                    "mountPath": "/runtime/bootstrap",
                                    "readOnly": True,
                                },
                                {"name": "pod-info", "mountPath": "/runtime/pod", "readOnly": True},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "inputs", "emptyDir": {"sizeLimit": "64Mi"}},
                        {"name": "workspace", "emptyDir": {"sizeLimit": request.workspace_size}},
                        {"name": "runtime-tmp", "emptyDir": {"sizeLimit": request.tmp_size}},
                        {
                            "name": "runtime-identity",
                            "projected": {
                                "defaultMode": 0o440,
                                "sources": [
                                    {
                                        "serviceAccountToken": {
                                            "audience": "agent-platform-runtime-bootstrap",
                                            "expirationSeconds": 600,
                                            "path": "bootstrap-token",
                                        }
                                    }
                                ],
                            },
                        },
                        {
                            "name": "pod-info",
                            "downwardAPI": {
                                "defaultMode": 0o440,
                                "items": [
                                    {
                                        "path": "pod-uid",
                                        "fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"},
                                    }
                                ],
                            },
                        },
                    ],
                },
            },
        },
    }
