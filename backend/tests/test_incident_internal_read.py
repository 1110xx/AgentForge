"""Incident vertical read path returns real business data through the mounted
Internal Runtime API.

SDD it-incident-investigate §3.2 (ResourceResolver) + §5.3 (resource parsing):
the Agent's ``remote_read_tool`` is proxied by the Control Plane through
``POST /internal/v1/runtime/tools/read``. Before this vertical the handler only
returned a ``[demo] …`` marker; business resolvers may now render agent-visible
content (real ticket / log / metric payloads, or the SDD ``kind: error`` JSON
for missing data) without failing the Attempt.

This gate proves, without an LLM:

  1. ``it-incident-investigate`` Runs create through the public API with an
     ``incident://`` resource ref;
  2. bootstrap issues a runtime capability and the read op is authorized;
  3. ticket / logs / metrics reads return the canonical business payloads;
  4. an unknown ticket yields the graceful error payload (not an HTTP failure);
  5. non-incident (synthetic) reads keep the legacy demo text (no regression).
"""
from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from enterprise_agent_platform import create_app
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.reference.incident import INCIDENT_DEMO_TICKET_ID
from enterprise_agent_platform.reference.local_stack import (
    REFERENCE_LOCAL_BEARER,
    REFERENCE_LOCAL_TENANT,
    create_container,
)

HEADERS = {"Authorization": REFERENCE_LOCAL_BEARER}


def _client():
    container = create_container()
    return TestClient(create_app(container)), container


def _create_incident_run(client: TestClient, ticket_id: str) -> str:
    response = client.post(
        "/v1/runs",
        headers={**HEADERS, "Idempotency-Key": f"incident-read-{ticket_id}"},
        json={
            "workflow_type": "it-incident-investigate",
            "intent": "分析工单#T20260907，定位支付服务 504 根因，输出报告",
            "resource_refs": [f"incident://ticket/{ticket_id}"],
            "host_context_ref": "reference-context:demo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


async def _pre_reserve(container, run_id: str):
    ctx = RequestContext(
        tenant_id=REFERENCE_LOCAL_TENANT,
        actor_id="incident-read-test",
        scopes=("runs:execute",),
        request_id="pre-reserve-incident",
    )
    unit = await container.store.get_primary_unit(ctx.tenant_id, run_id)
    checkpoint = await container.store.get_checkpoint(
        ctx.tenant_id, unit.current_checkpoint_id
    )
    return await container.control.reserve_attempt(
        ctx,
        unit.execution_unit_id,
        checkpoint.checkpoint_id,
        unit.version,
        transition_key="incident-read-test",
    )


def test_incident_run_creation_rejects_unknown_ticket() -> None:
    client, _ = _client()
    with client:
        response = client.post(
            "/v1/runs",
            headers={**HEADERS, "Idempotency-Key": "incident-read-unknown"},
            json={
                "workflow_type": "it-incident-investigate",
                "intent": "分析不存在工单",
                "resource_refs": ["incident://ticket/T99999999"],
            },
        )
        assert response.status_code in (404, 422), response.text


def _synthetic_only_client():
    from enterprise_agent_platform import create_in_memory_container
    from enterprise_agent_platform.reference.local_stack import (
        ReferenceAllowAllPolicy,
        ReferenceHostContextVerifier,
        ReferenceLocalAuth,
        ReferenceSyntheticResources,
    )
    from enterprise_agent_platform.reference.session import InMemoryRunSessionProvider

    container = create_in_memory_container(
        auth_context_provider=ReferenceLocalAuth(),
        resource_resolver=ReferenceSyntheticResources(),
        host_context_verifier=ReferenceHostContextVerifier(),
        policy_context_provider=ReferenceAllowAllPolicy(),
        run_sessions=InMemoryRunSessionProvider(),
    )
    return TestClient(create_app(container))


def test_scheme_uri_rejected_without_declared_resolver_scheme() -> None:
    """Anti-smuggling default is preserved: only a resolver that declares the
    scheme may receive scheme URIs."""
    with _synthetic_only_client() as client:
        response = client.post(
            "/v1/runs",
            headers={
                **HEADERS,
                "Idempotency-Key": "incident-undeclared-scheme",
            },
            json={
                "workflow_type": "it-incident-investigate",
                "intent": "分析工单",
                "resource_refs": [f"incident://ticket/{INCIDENT_DEMO_TICKET_ID}"],
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "INVALID_OPAQUE_REFERENCE"


def test_arbitrary_scheme_rejected_even_with_incident_resolver() -> None:
    with _client()[0] as client:
        response = client.post(
            "/v1/runs",
            headers={**HEADERS, "Idempotency-Key": "incident-http-scheme"},
            json={
                "workflow_type": "it-incident-investigate",
                "intent": "分析工单",
                "resource_refs": ["http://evil.example/steal"],
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "INVALID_OPAQUE_REFERENCE"


def test_read_tool_returns_real_incident_payloads() -> None:
    client, container = _client()
    with client:
        run_id = _create_incident_run(client, INCIDENT_DEMO_TICKET_ID)
        reservation = asyncio.run(_pre_reserve(container, run_id))
        attempt = reservation.attempt

        bootstrap = client.post(
            "/internal/v1/runtime/bootstrap",
            headers={"Authorization": f"Bearer projected:{REFERENCE_LOCAL_TENANT}"},
            json={
                "pod_uid": "pod-incident-1",
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
            },
        )
        assert bootstrap.status_code == 200, bootstrap.text
        runtime_token = bootstrap.json()["runtime_token"]
        runtime_headers = {"Authorization": f"Bearer {runtime_token}"}
        # The Pod signs every op with the full bootstrap subject, execution_unit_id
        # included; the wire models must accept it (regression for live 422s).
        unit = asyncio.run(
            container.store.get_primary_unit(REFERENCE_LOCAL_TENANT, run_id)
        )
        subject = {
            "tenant_id": REFERENCE_LOCAL_TENANT,
            "run_id": run_id,
            "attempt_id": attempt.attempt_id,
            "generation": attempt.generation,
            "execution_unit_id": unit.execution_unit_id,
        }

        # ── ticket ──
        ticket_read = client.post(
            "/internal/v1/runtime/tools/read",
            headers=runtime_headers,
            json={
                **subject,
                "tool_name": "incident.ticket.read",
                "arguments_ref": f"incident://ticket/{INCIDENT_DEMO_TICKET_ID}",
            },
        )
        assert ticket_read.status_code == 200, ticket_read.text
        ticket = json.loads(ticket_read.json()["content"])
        assert ticket["kind"] == "incident_ticket"
        assert ticket["ticket_id"] == INCIDENT_DEMO_TICKET_ID
        assert ticket["affected_services"] == ["pay-service"]

        # ── logs (with explicit window) ──
        logs_read = client.post(
            "/internal/v1/runtime/tools/read",
            headers=runtime_headers,
            json={
                **subject,
                "tool_name": "incident.logs.read",
                "arguments_ref": (
                    "incident://logs/pay-service?start_ts=2026-09-07T08:18:00Z"
                    "&end_ts=2026-09-07T08:24:00Z"
                ),
            },
        )
        assert logs_read.status_code == 200, logs_read.text
        logs = json.loads(logs_read.json()["content"])
        assert logs["kind"] == "incident_logs"
        assert logs["service_name"] == "pay-service"
        messages = [row["message"] for row in logs["logs"]]
        assert any("db slow query" in message for message in messages)

        # ── metrics ──
        metrics_read = client.post(
            "/internal/v1/runtime/tools/read",
            headers=runtime_headers,
            json={
                **subject,
                "tool_name": "incident.metrics.read",
                "arguments_ref": "incident://metrics/pay-service",
            },
        )
        assert metrics_read.status_code == 200, metrics_read.text
        metrics = json.loads(metrics_read.json()["content"])
        assert metrics["kind"] == "incident_metrics"
        cpu = next(
            item for item in metrics["series"] if item["metric"] == "cpu_usage_pct"
        )
        assert max(point["value"] for point in cpu["datapoints"]) >= 90

        # ── unknown ticket: graceful error payload, not an HTTP failure ──
        missing_read = client.post(
            "/internal/v1/runtime/tools/read",
            headers=runtime_headers,
            json={
                **subject,
                "tool_name": "incident.ticket.read",
                "arguments_ref": "incident://ticket/T99999999",
            },
        )
        assert missing_read.status_code == 200, missing_read.text
        error = json.loads(missing_read.json()["content"])
        assert error == {"kind": "error", "code": "NOT_FOUND", "message": error["message"]}

        # ── synthetic reads unchanged (legacy demo text preserved) ──
        synthetic_read = client.post(
            "/internal/v1/runtime/tools/read",
            headers=runtime_headers,
            json={
                **subject,
                "tool_name": "synthetic.results.read",
                "arguments_ref": "synthetic-case:case-42",
            },
        )
        assert synthetic_read.status_code == 200, synthetic_read.text
        assert "[demo]" in synthetic_read.json()["content"]


def test_read_tool_rejects_unauthenticated_call() -> None:
    client, container = _client()
    with client:
        run_id = _create_incident_run(client, INCIDENT_DEMO_TICKET_ID)
        reservation = asyncio.run(_pre_reserve(container, run_id))
        attempt = reservation.attempt
        response = client.post(
            "/internal/v1/runtime/tools/read",
            headers={"Authorization": "Bearer runtime-token:forged"},
            json={
                "tenant_id": REFERENCE_LOCAL_TENANT,
                "run_id": run_id,
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
                "tool_name": "incident.ticket.read",
                "arguments_ref": f"incident://ticket/{INCIDENT_DEMO_TICKET_ID}",
            },
        )
        assert response.status_code == 401, response.text
