"""Attempt Job pod-spec knobs for the sandbox network profile.

``hostNetwork`` + ``dnsPolicy`` on Attempt Jobs are opt-in (default off) so
the production pod spec is unchanged unless a deployment opts in — the
egress workaround used on hosts whose Pod overlay cannot reach external or
node-network endpoints (see ``AGENT_PLATFORM_SANDBOX_HOST_NETWORK``).
"""
from __future__ import annotations

from enterprise_agent_platform.execution.job_spec import (
    AttemptJobRequest,
    build_attempt_job,
)


def _request(**overrides: object) -> AttemptJobRequest:
    base: dict[str, object] = {
        "tenant_id": "tenant-1",
        "run_id": "run_1",
        "execution_unit_id": "unit_1",
        "attempt_id": "attempt_1",
        "generation": 1,
        "namespace": "agent-platform-sandbox",
        "image": "localhost:5001/repo/runtime@sha256:"
        + "a" * 64,
        "control_plane_url": "http://api.svc:8000",
        "cpu_request": "50m",
        "cpu_limit": "500m",
        "memory_request": "64Mi",
        "memory_limit": "256Mi",
        "workspace_size": "128Mi",
        "tmp_size": "64Mi",
        "active_deadline_seconds": 300,
    }
    base.update(overrides)
    return AttemptJobRequest(**base)  # type: ignore[arg-type]


def test_default_spec_has_no_host_network() -> None:
    spec = build_attempt_job(_request())
    pod_spec = spec["spec"]["template"]["spec"]
    assert "hostNetwork" not in pod_spec
    assert "dnsPolicy" not in pod_spec


def test_opt_in_sets_host_network_and_dns_policy() -> None:
    spec = build_attempt_job(
        _request(host_network=True, dns_policy="ClusterFirstWithHostNet")
    )
    pod_spec = spec["spec"]["template"]["spec"]
    assert pod_spec["hostNetwork"] is True
    assert pod_spec["dnsPolicy"] == "ClusterFirstWithHostNet"
