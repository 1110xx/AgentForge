"""M6-A2 live-streaming bridge over the HTTP runtime Internal API.

SDD it-incident-investigate v1.3 §13.3 (A2, final target state): the Pod
runtime (HttpRunner) must report pi-agent-core turns/tools to the Control
Plane the same way the pipe/in-process path does, otherwise the public event
log stays lifecycle-only and the frontend activity panel is empty during live
runs (the user-facing gap behind §13.0 feedback ①).

This gate proves, without an LLM:

  1. ``emit_event`` (POST /internal/v1/runtime/events) appends an allowed
     durable bridge event (``agent.turn.completed``) — it shows up in the
     public run event log with the attempt bound;
  2. unknown event types are rejected by the bridge allowlist;
  3. bridge events on a non-active run are rejected (RUN_NOT_ACTIVE);
  4. ``stream_chunk`` (POST /internal/v1/runtime/chunks) lands in the shared
     in-memory relay the SSE endpoint drains (ephemeral, never persisted).

Wire discipline: every request signs the full bootstrap subject (including
``execution_unit_id``) exactly like the Pod does.
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

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
    return TestClient(create_app_of(container)), container


def create_app_of(container):
    from enterprise_agent_platform import create_app

    return create_app(container)


def _create_incident_run(client: TestClient, ticket_id: str) -> str:
    response = client.post(
        "/v1/runs",
        headers={**HEADERS, "Idempotency-Key": f"bridge-{ticket_id}"},
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
        actor_id="bridge-test",
        scopes=("runs:execute",),
        request_id="pre-reserve-bridge",
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
        transition_key="bridge-test",
    )


def _bootstrap(client: TestClient, container, run_id: str):
    """Create the run, reserve an attempt and return (attempt, runtime_headers, subject)."""
    reservation = asyncio.run(_pre_reserve(container, run_id))
    attempt = reservation.attempt
    bootstrap = client.post(
        "/internal/v1/runtime/bootstrap",
        headers={"Authorization": f"Bearer projected:{REFERENCE_LOCAL_TENANT}"},
        json={
            "pod_uid": "pod-bridge-1",
            "attempt_id": attempt.attempt_id,
            "generation": attempt.generation,
        },
    )
    assert bootstrap.status_code == 200, bootstrap.text
    runtime_token = bootstrap.json()["runtime_token"]
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
    return attempt, {"Authorization": f"Bearer {runtime_token}"}, subject


def test_emit_event_appends_durable_turn_event() -> None:
    client, container = _client()
    with client:
        run_id = _create_incident_run(client, INCIDENT_DEMO_TICKET_ID)
        attempt, runtime_headers, subject = _bootstrap(client, container, run_id)

        response = client.post(
            "/internal/v1/runtime/events",
            headers=runtime_headers,
            json={
                **subject,
                "event_type": "agent.turn.completed",
                "payload": {
                    "kind": "agent.turn.completed",
                    "turn_seq": 1,
                    "thinking": "",
                    "message_text": "已定位根因：支付服务与账单服务超时。",
                    "tool_calls": [
                        {
                            "call_id": "call_1",
                            "tool_name": "incident.ticket.read",
                            "status": "succeeded",
                            "is_error": False,
                        }
                    ],
                },
            },
        )
        assert response.status_code == 200, response.text

        events = client.get(
            f"/v1/runs/{run_id}/events?limit=100",
            headers=HEADERS,
        ).json()["events"]
        turn_events = [
            e
            for e in events
            if e["event_type"] == "agent.turn.completed"
        ]
        assert len(turn_events) == 1
        payload = turn_events[0]["payload"]
        assert payload["turn_seq"] == 1
        assert payload["tool_calls"][0]["tool_name"] == "incident.ticket.read"
        assert turn_events[0]["attempt_id"] == attempt.attempt_id


def test_emit_event_rejects_unknown_type_and_forged_payload() -> None:
    client, container = _client()
    with client:
        run_id = _create_incident_run(client, INCIDENT_DEMO_TICKET_ID)
        _, runtime_headers, subject = _bootstrap(client, container, run_id)

        unknown = client.post(
            "/internal/v1/runtime/events",
            headers=runtime_headers,
            json={
                **subject,
                "event_type": "run.custom.smuggle",
                "payload": {"kind": "run.custom.smuggle"},
            },
        )
        assert unknown.status_code == 400, unknown.text
        assert "INVALID_EVENT_TYPE" in unknown.text or "EVENT_TYPE_NOT_ALLOWED" in unknown.text

        malformed = client.post(
            "/internal/v1/runtime/events",
            headers=runtime_headers,
            json={
                **subject,
                "event_type": "agent.turn.completed",
                "payload": {"kind": "not-a-real-kind"},
            },
        )
        assert malformed.status_code == 422, malformed.text


def test_emit_event_rejected_when_run_not_active() -> None:
    client, container = _client()
    with client:
        run_id = _create_incident_run(client, INCIDENT_DEMO_TICKET_ID)
        _, runtime_headers, subject = _bootstrap(client, container, run_id)
        # Fail the attempt so the run leaves RUNNING (terminal).
        failure = client.post(
            "/internal/v1/runtime/failures",
            headers=runtime_headers,
            json={**subject, "reason_code": "TEST_FAIL", "retryable": False},
        )
        assert failure.status_code == 200, failure.text
        late = client.post(
            "/internal/v1/runtime/events",
            headers=runtime_headers,
            json={
                **subject,
                "event_type": "agent.turn.completed",
                "payload": {
                    "kind": "agent.turn.completed",
                    "turn_seq": 1,
                    "message_text": "late",
                },
            },
        )
        assert late.status_code == 409, late.text


def test_stream_chunk_reaches_shared_relay() -> None:
    client, container = _client()
    with client:
        run_id = _create_incident_run(client, INCIDENT_DEMO_TICKET_ID)
        _, runtime_headers, subject = _bootstrap(client, container, run_id)

        response = client.post(
            "/internal/v1/runtime/chunks",
            headers=runtime_headers,
            json={
                **subject,
                "chunk": {"kind": "thinking.delta", "delta": "先读工单…"},
            },
        )
        assert response.status_code == 200, response.text

        relay = container.chunk_streamer
        drained = relay.drain(run_id, limit=10) if relay is not None else []
        assert any(c.get("kind") == "thinking.delta" for c in drained)
        # run_id is stamped by the bridge so the SSE drain can key the run.
        assert all(c.get("run_id") == run_id for c in drained)
