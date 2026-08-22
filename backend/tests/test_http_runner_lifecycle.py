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

# Realistic pi-agent-core Agent snapshot (what ``runtime.py`` exports at
# TurnEnd/AgentEnd — stable fields only: system_prompt/model/thinking_level/
# tools/messages). Hydration assertions below rely on it round-tripping:
# commit_final → terminal Checkpoint → restore on a subsequent Attempt.
_AGENT_STATE = {
    "system_prompt": "## Task Intent\n\nAnalyze a portable synthetic resource",
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
            "content": [{"type": "text", "text": "Summarize the synthetic case."}],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "The synthetic case is portable and self-contained."}
            ],
        },
    ],
}
_AGENT_STATE_SCHEMA = "pi-agent-core/v1"


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


async def _pre_reserve(container, run_id: str, *, transition_key: str = "http-runner-test"):
    ctx = RequestContext(
        tenant_id=REFERENCE_LOCAL_TENANT,
        actor_id="http-runner-test",
        scopes=("runs:execute",),
        request_id=f"pre-reserve-http:{transition_key}",
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
        transition_key=transition_key,
    )


def _bootstrap(client: TestClient, attempt) -> tuple[dict, dict]:
    """Bootstrap an Attempt through the Internal API and return (identity, lease)."""
    bootstrap = client.post(
        "/internal/v1/runtime/bootstrap",
        headers={"Authorization": f"Bearer projected:{REFERENCE_LOCAL_TENANT}"},
        json={
            "pod_uid": f"pod-http-{attempt.attempt_id[-8:]}",
            "attempt_id": attempt.attempt_id,
            "generation": attempt.generation,
        },
    )
    assert bootstrap.status_code == 200, bootstrap.text
    identity = bootstrap.json()
    assert identity["runtime_token"] == f"runtime-token:{attempt.attempt_id}"
    subject = {
        "tenant_id": REFERENCE_LOCAL_TENANT,
        "run_id": attempt.run_id,
        "execution_unit_id": attempt.execution_unit_id,
        "attempt_id": attempt.attempt_id,
        "generation": attempt.generation,
    }
    lease = {
        **subject,
        "lease_owner": identity["lease_owner"],
        "lease_version": identity["lease_version"],
    }
    return identity, lease


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
            "execution_unit_id": attempt.execution_unit_id,
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

        # ── 6. commit_final: terminal snapshot + transition (Run → SUCCEEDED) ──
        # The Pod sends its exported Agent state on AgentEnd (runtime.py Step 7);
        # the terminal Checkpoint is the cursor a follow-up / rerun Attempt
        # restores from, so it must carry the same snapshot history.
        final = client.post(
            "/internal/v1/runtime/checkpoints/final",
            headers=runtime_headers,
            json={
                **lease,
                "summary": "Synthetic analysis completed.",
                "agent_state": _AGENT_STATE,
                "agent_state_schema_version": _AGENT_STATE_SCHEMA,
            },
        )
        assert final.status_code == 200, final.text

        # The terminal Checkpoint (unit's current cursor) must persist the
        # Agent snapshot — restore hydration depends on it.
        unit_after = asyncio.run(
            container.store.get_primary_unit(REFERENCE_LOCAL_TENANT, run_id)
        )
        terminal = asyncio.run(
            container.store.get_checkpoint(
                REFERENCE_LOCAL_TENANT, unit_after.current_checkpoint_id
            )
        )
        assert terminal.agent_state == _AGENT_STATE, (
            "commit_final must persist the terminal Agent snapshot"
        )
        assert terminal.agent_state_schema_version == _AGENT_STATE_SCHEMA

        run_view = client.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
        assert run_view["status"] == "SUCCEEDED", run_view

        # Attempt must have reached SUCCEEDED — read through the public
        # attempts endpoint (SDD §10.1, no store direct access).
        attempts = client.get(f"/v1/runs/{run_id}/attempts", headers=HEADERS).json()
        assert attempts["total_count"] == 1, attempts
        assert attempts["records"][0]["attempt_id"] == attempt.attempt_id
        assert attempts["records"][0]["status"] == "SUCCEEDED", attempts
        assert attempts["records"][0]["ended_at"], "succeeded attempt must be ended"

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


def test_http_restore_hydrates_agent_state_across_rounds() -> None:
    """Restore hydration closure: a subsequent Attempt rehydrates history.

    Phase 3 last gap: the HTTP adapter used to throw away ``request.agent_state``
    on commit_final (writing ``{}`` + ``http-runtime/v0``), so the terminal
    Checkpoint — the cursor any follow-up / rerun Attempt restores from — had
    no Agent snapshot and the fresh Pod started with a blank Agent. This test
    pins the closed loop:

    1. full HTTP lifecycle with turn + final snapshots → Run SUCCEEDED;
    2. a follow-up reactivates the Run (RECOVERING + PENDING durable record);
    3. a new Attempt (generation+1) bootstraps and restores via the Internal
       API;
    4. restore returns the hydrated ``agent_state`` (full conversation history)
       plus the injected ``followup_question``.
    """
    client, container = _client()
    with client:
        run_id = _create_run(client, "http-hydration-1")
        reservation = asyncio.run(_pre_reserve(container, run_id))
        attempt = reservation.attempt

        identity, lease = _bootstrap(client, attempt)
        runtime_headers = {"Authorization": f"Bearer {identity['runtime_token']}"}

        restore = client.post(
            "/internal/v1/runtime/restore", headers=runtime_headers, json=lease
        )
        assert restore.status_code == 200, restore.text
        assert restore.json()["agent_state"] == {}, (
            "fresh run restores an empty Agent snapshot"
        )

        heartbeat = client.post(
            "/internal/v1/runtime/heartbeat", headers=runtime_headers, json=lease
        )
        refreshed = heartbeat.json()
        lease["lease_version"] = refreshed["lease_version"]

        turn = client.post(
            "/internal/v1/runtime/checkpoints",
            headers=runtime_headers,
            json={
                **lease,
                "agent_state": _AGENT_STATE,
                "agent_state_schema_version": _AGENT_STATE_SCHEMA,
            },
        )
        assert turn.status_code == 200, turn.text

        final = client.post(
            "/internal/v1/runtime/checkpoints/final",
            headers=runtime_headers,
            json={
                **lease,
                "summary": "Synthetic analysis completed.",
                "agent_state": _AGENT_STATE,
                "agent_state_schema_version": _AGENT_STATE_SCHEMA,
            },
        )
        assert final.status_code == 200, final.text
        run_view = client.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
        assert run_view["status"] == "SUCCEEDED", run_view

        # ── Subsequent round: queue a follow-up on the terminal Run ──
        ctx = RequestContext(
            tenant_id=REFERENCE_LOCAL_TENANT,
            actor_id="http-runner-test",
            scopes=("runs:execute", "runs:write"),
            request_id="followup-hydration",
        )
        followup = asyncio.run(
            container.control.queue_followup(
                ctx,
                run_id,
                question="Why did the summary omit case 3?",
                client_followup_id="http-hydration-followup",
            )
        )
        assert followup.status == "PENDING", followup.status
        recovering = asyncio.run(container.store.get_run(REFERENCE_LOCAL_TENANT, run_id))
        assert recovering.status.value == "RECOVERING", recovering.status

        # ── Reserve + bootstrap + restore the fresh Attempt (generation+1) ──
        next_reservation = asyncio.run(
            _pre_reserve(container, run_id, transition_key="http-runner-test-followup")
        )
        assert next_reservation.attempt.generation == attempt.generation + 1
        # The public attempts endpoint exposes both generations (SDD §10.1
        # — tests need no store direct access).
        attempts = client.get(f"/v1/runs/{run_id}/attempts", headers=HEADERS).json()
        assert attempts["total_count"] == 2, attempts
        generations = sorted(item["attempt_id"] for item in attempts["records"])
        assert all(generations), attempts
        next_identity, next_lease = _bootstrap(client, next_reservation.attempt)
        next_headers = {"Authorization": f"Bearer {next_identity['runtime_token']}"}

        restore_fresh = client.post(
            "/internal/v1/runtime/restore", headers=next_headers, json=next_lease
        )
        assert restore_fresh.status_code == 200, restore_fresh.text
        rehydrated = restore_fresh.json()
        assert rehydrated["checkpoint_state"] == "COMMITTED"
        assert rehydrated["agent_state"] == _AGENT_STATE, (
            "subsequent-round restore must rehydrate the terminal Agent snapshot"
        )
        assert rehydrated["agent_state_schema_version"] == _AGENT_STATE_SCHEMA
        assert rehydrated["workflow_cursor"].get("followup_question") == (
            "Why did the summary omit case 3?"
        ), "restore cursor must carry the queued follow-up question"
        assert rehydrated["workflow_cursor"].get("summary") == (
            "Synthetic analysis completed."
        )


def test_attempts_endpoint_requires_auth_and_run_exists() -> None:
    client, _ = _client()
    with client:
        # Unauthenticated access must be rejected before route logic.
        missing_auth = client.get("/v1/runs/run-any/attempts")
        assert missing_auth.status_code in {401, 403}, missing_auth.text

        run_id = _create_run(client, "attempts-endpoint-1")
        # A freshly created Run has no Attempt yet (the scheduler reserves one
        # before execution) — empty but valid history.
        listed = client.get(f"/v1/runs/{run_id}/attempts", headers=HEADERS)
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["schema_version"] == "attempt-history-page/v1"
        assert body["run_id"] == run_id
        assert body["total_count"] == 0 and body["records"] == [], body

        # Unknown Run → 404.
        missing = client.get("/v1/runs/run-does-not-exist/attempts", headers=HEADERS)
        assert missing.status_code == 404, missing.text


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
                "execution_unit_id": attempt.execution_unit_id,
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


def test_http_bootstrap_grant_subject_reaches_restore() -> None:
    """AgentRuntime must sign HTTP ops with the bootstrap grant's subject.

    L3 gate catch: the Pod's restore failed 422 because the RuntimeContext was
    built without tenant_id/run_id/execution_unit_id from the grant — the
    Internal API rejects the empty-subject body. This test pins the contract:
    whatever the BootstrapClient claims must be what restore receives.
    """
    import asyncio

    from enterprise_agent_platform.execution.runtime import (
        AgentRuntime,
        BootstrapGrant,
        RuntimeCheckpoint,
        RuntimeContext,
    )
    from enterprise_agent_platform.persistence.protocol import PlatformError

    seen: dict[str, RuntimeContext] = {}

    class _Bootstrap:
        async def claim(
            self, *, bootstrap_token: str, pod_uid: str, attempt_id: str, generation: int
        ) -> BootstrapGrant:
            return BootstrapGrant(
                runtime_token="runtime-token:a",
                lease_owner="http-runtime:a",
                lease_version=2,
                expires_at="2026-08-21T00:00:00Z",
                tenant_id="tenant-http",
                run_id="run-http",
                execution_unit_id="unit-http",
            )

    class _Control:
        async def restore(self, context: RuntimeContext) -> RuntimeCheckpoint:
            seen["ctx"] = context
            raise PlatformError("stop-after-restore", "test boundary")

    class _Identity:
        async def provide(self) -> tuple[str, str]:
            return ("projected:tenant-http", "pod-1")

    runtime = AgentRuntime(
        bootstrap=_Bootstrap(),
        control=_Control(),
        identity_provider=_Identity(),
    )
    exit_code = asyncio.run(runtime.run(attempt_id="attempt-http", generation=1))
    assert exit_code == 78, "restore failure at the test boundary is exit 78"

    ctx = seen["ctx"]
    assert ctx.tenant_id == "tenant-http"
    assert ctx.run_id == "run-http"
    assert ctx.execution_unit_id == "unit-http"
    assert ctx.attempt_id == "attempt-http"
    assert ctx.lease_owner == "http-runtime:a"
    assert ctx.lease_version == 2


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
                "execution_unit_id": "unit-missing",
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