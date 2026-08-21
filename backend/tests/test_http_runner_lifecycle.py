"""HttpRunner end-to-end: a Pod-shaped client drives the mounted Internal API.

Phase 3 (HttpRunner 串联): the K8s Job container runs
``python -m enterprise_agent_platform.execution.runtime`` which talks HTTP to
the Control-Plane Internal API. This test replays that full lifecycle against
the mounted app (fastapi/internal_adapter.py) as an ASGI client — no pipes, no
cluster: exactly what the Pod does, minus the container.

       public POST /v1/runs → Run QUEUED
       scheduler-style reserve_attempt → Attempt PROVISIONING + Lease RESERVED
       bootstrap (projected:{tenant}) → identity + **Lease ACTIVE** + Run RUNNING
       restore → checkpoint cursor + agent_state snapshot
       heartbeat → refreshed lease_version (CAS prerequisite)
       model_call → LLM response proxied through RunSessionProvider
       commit_checkpoint (turn) → checkpoint_seq advances, agent snapshot stored
       commit_final → Run/Attempt SUCCEEDED + events written
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from enterprise_agent_platform import create_app
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.reference.local_stack import (
    REFERENCE_LOCAL_BEARER,
    REFERENCE_LOCAL_TENANT,
    create_container,
)

HEADERS = {"Authorization": REFERENCE_LOCAL_BEARER}


def _client() -> tuple[TestClient, object]:
    container = create_container()
    return TestClient(create_app(container)), container


def _create_run(client: TestClient, idempotency_key: str) -> str:
    response = client.post(
        "/v1/runs",
        headers={**HEADERS, "Idempotency-Key": idempotency_key},
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


async def _pre_reserve(container, run_id: str):
    ctx = RequestContext(
        tenant_id=REFERENCE_LOCAL_TENANT,
        actor_id="http-runner-test",
        scopes=("runs:execute",),
        request_id="pre-reserve-http",
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
        transition_key="http-runner-test",
    )


def test_http_runner_full_lifecycle() -> None:
    client, container = _client()
    with client:
        run_id = _create_run(client, "http-runner-lifecycle-1")
        reservation = asyncio.run(_pre_reserve(container, run_id))
        attempt = reservation.attempt

        # ── 1. bootstrap: identity + Lease activation ──
        bootstrap = client.post(
            "/internal/v1/runtime/bootstrap",
            headers={"Authorization": f"Bearer projected:{REFERENCE_LOCAL_TENANT}"},
            json={
                "pod_uid": "pod-http-1",
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
            },
        )
        assert bootstrap.status_code == 200, bootstrap.text
        identity = bootstrap.json()
        assert identity["runtime_token"] == f"runtime-token:{attempt.attempt_id}"
        assert identity["lease_owner"] == f"http-runtime:{attempt.attempt_id}"
        assert identity["lease_version"] >= 2, "bootstrap must activate the Lease"
        assert identity["expires_at"], "active Lease must have an expiry"

        runtime_headers = {"Authorization": f"Bearer {identity['runtime_token']}"}
        subject = {
            "tenant_id": REFERENCE_LOCAL_TENANT,
            "run_id": run_id,
            "attempt_id": attempt.attempt_id,
            "generation": attempt.generation,
        }
        lease = {
            **subject,
            "lease_owner": identity["lease_owner"],
            "lease_version": identity["lease_version"],
        }

        # Run must now be RUNNING (bootstrap transitioned it).
        run_view = client.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
        assert run_view["status"] == "RUNNING", run_view

        # ── 2. restore: checkpoint cursor + agent snapshot ──
        restore = client.post(
            "/internal/v1/runtime/restore",
            headers=runtime_headers,
            json=lease,
        )
        assert restore.status_code == 200, restore.text
        checkpoint = restore.json()
        assert checkpoint["checkpoint_state"] == "COMMITTED"
        assert checkpoint["workflow_cursor"]["intent"].startswith(
            "Analyze a portable synthetic resource"
        )

        # ── 3. heartbeat: lease_version must advance ──
        heartbeat = client.post(
            "/internal/v1/runtime/heartbeat",
            headers=runtime_headers,
            json=lease,
        )
        assert heartbeat.status_code == 200, heartbeat.text
        refreshed = heartbeat.json()
        assert refreshed["lease_version"] > identity["lease_version"]
        # The Pod keeps its refreshed context (AgentRuntime does the same); all
        # later CAS-signature writes must carry the fresh lease_version.
        lease["lease_version"] = refreshed["lease_version"]

        # ── 4. model_call: proxied through the Control-Plane provider ──
        model_call = client.post(
            "/internal/v1/runtime/model-call",
            headers=runtime_headers,
            json={
                **subject,
                "model": {
                    "api": "deepseek",
                    "provider": "deepseek",
                    "id": "deepseek-chat",
                },
                "system_prompt": "## Task Intent\n\nAnalyze the resource.",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Summarize the synthetic case."}
                        ],
                    }
                ],
                "tools": [],
                "options": {"temperature": 0.7, "max_tokens": 512, "reasoning": 0},
            },
        )
        assert model_call.status_code == 200, model_call.text
        llm = model_call.json()
        assert llm["content"], "model_call must return content blocks"
        assert llm["stop_reason"], "model_call must return a stop_reason"

        # ── 5. turn-level checkpoint (TurnEnd safe snapshot boundary) ──
        turn = client.post(
            "/internal/v1/runtime/checkpoints",
            headers=runtime_headers,
            json={
                **lease,
                "agent_state": {
                    "system_prompt": "## Task Intent\n\nAnalyze the resource.",
                    "model": {
                        "api": "deepseek",
                        "provider": "deepseek",
                        "id": "deepseek-chat",
                    },
                    "thinking_level": None,
                    "tools": [],
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Summarize the synthetic case."}
                            ],
                        }
                    ],
                },
                "agent_state_schema_version": "pi-agent-core/v1",
            },
        )
        assert turn.status_code == 200, turn.text
        assert turn.json()["status"] == "committed"
        assert turn.json()["result_ref"], "turn checkpoint must return checkpoint_id"

        # ── 6. commit_final: terminal transition (Run → SUCCEEDED) ──
        final = client.post(
            "/internal/v1/runtime/checkpoints/final",
            headers=runtime_headers,
            json={
                **lease,
                "summary": "Synthetic analysis completed.",
            },
        )
        assert final.status_code == 200, final.text

        run_view = client.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
        assert run_view["status"] == "SUCCEEDED", run_view

        # Attempt must have reached SUCCEEDED (checked through the store;
        # there is no public attempts endpoint).
        final_attempt = asyncio.run(
            container.store.get_attempt(REFERENCE_LOCAL_TENANT, attempt.attempt_id)
        )
        assert final_attempt.status.value == "SUCCEEDED", final_attempt.status

        # Event timeline includes the full lifecycle.
        events = client.get(
            f"/v1/runs/{run_id}/events", headers=HEADERS
        ).json()
        event_types = [event["event_type"] for event in events["events"]]
        for expected in (
            "run.created",
            "attempt.lifecycle",
            "run.status.changed",
        ):
            assert expected in event_types, (expected, event_types)


def test_http_runner_rejects_wrong_runtime_token() -> None:
    client, container = _client()
    with client:
        run_id = _create_run(client, "http-runner-authz-1")
        reservation = asyncio.run(_pre_reserve(container, run_id))
        attempt = reservation.attempt

        bootstrap = client.post(
            "/internal/v1/runtime/bootstrap",
            headers={"Authorization": f"Bearer projected:{REFERENCE_LOCAL_TENANT}"},
            json={
                "pod_uid": "pod-http-2",
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
            },
        )
        assert bootstrap.status_code == 200, bootstrap.text

        # A forged runtime token must be rejected (401) before any op runs.
        forged = client.post(
            "/internal/v1/runtime/heartbeat",
            headers={"Authorization": "Bearer runtime-token:not-the-attempt"},
            json={
                "tenant_id": REFERENCE_LOCAL_TENANT,
                "run_id": run_id,
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
                "lease_owner": "http-runtime:forged",
                "lease_version": 1,
            },
        )
        assert forged.status_code == 401, forged.text

        # A projected token must carry the tenant prefix.
        invalid = client.post(
            "/internal/v1/runtime/bootstrap",
            headers={"Authorization": "Bearer some-random-jwt"},
            json={
                "pod_uid": "pod-http-3",
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
            },
        )
        assert invalid.status_code == 401, invalid.text


def test_model_call_rejects_unknown_op_scope() -> None:
    client, _ = _client()
    with client:
        # model_call without a bound runtime identity must fail closed.
        response = client.post(
            "/internal/v1/runtime/model-call",
            headers={"Authorization": "Bearer runtime-token:missing"},
            json={
                "tenant_id": REFERENCE_LOCAL_TENANT,
                "run_id": "run-missing",
                "attempt_id": "attempt-missing",
                "generation": 1,
                "model": {"id": "deepseek-chat", "api": "deepseek", "provider": "deepseek"},
                "system_prompt": "",
                "messages": [],
                "tools": [],
                "options": {"temperature": 0.7},
            },
        )
        assert response.status_code == 401, response.text