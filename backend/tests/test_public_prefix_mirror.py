"""The public /v1 router is reachable at both /v1 and /api/agent-platform/v1.

Same-origin SPA deployments (the embedded-host baseUrl "/api/agent-platform/")
go through a gateway-less ingress pass-through: the path prefix is NOT
rewritten before the API pod, so the SPA's /api/agent-platform/v1/runs calls
must hit the same handlers as the documented /v1/runs. Health/metrics already
live under /api/agent-platform (entrypoint + app.py), which is why the mirror
mount is the consistent fix rather than an ingress rewrite rule.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from enterprise_agent_platform import create_app
from enterprise_agent_platform.reference.local_stack import (
    REFERENCE_LOCAL_BEARER,
    create_container,
)

HEADERS = {"Authorization": REFERENCE_LOCAL_BEARER}


def _client() -> TestClient:
    return TestClient(create_app(create_container()))


def _create_body() -> dict[str, object]:
    return {
        "workflow_type": "synthetic-analysis",
        "intent": "Analyze a portable synthetic resource",
        "resource_refs": ["synthetic-case:case-42"],
        "parameters": {"analysis_mode": "summary", "max_items": 10},
        "host_context_ref": "reference-context:demo",
    }


def test_prefixed_public_create_run_matches_root_contract():
    client = _client()
    root = client.post(
        "/v1/runs",
        headers={**HEADERS, "Idempotency-Key": "prefix-mirror-root"},
        json=_create_body(),
    )
    assert root.status_code == 201, root.text
    prefixed = client.post(
        "/api/agent-platform/v1/runs",
        headers={**HEADERS, "Idempotency-Key": "prefix-mirror-prefixed"},
        json=_create_body(),
    )
    assert prefixed.status_code == 201, prefixed.text
    body = prefixed.json()
    assert body["schema_version"] == "run-view-snapshot/v1"
    assert body["view"]["workflow_type"] == "synthetic-analysis"


def test_prefixed_public_read_and_events_are_mounted():
    client = _client()
    created = client.post(
        "/api/agent-platform/v1/runs",
        headers={**HEADERS, "Idempotency-Key": "prefix-mirror-read"},
        json=_create_body(),
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    snapshot = client.get(f"/api/agent-platform/v1/runs/{run_id}", headers=HEADERS)
    assert snapshot.status_code == 200, snapshot.text
    events = client.get(
        f"/api/agent-platform/v1/runs/{run_id}/events?cursor=0", headers=HEADERS
    )
    assert events.status_code == 200, events.text


def test_prefix_mirror_still_enforces_auth():
    client = _client()
    response = client.post(
        "/api/agent-platform/v1/runs",
        headers={"Idempotency-Key": "prefix-mirror-noauth"},
        json=_create_body(),
    )
    assert response.status_code == 401, response.text
