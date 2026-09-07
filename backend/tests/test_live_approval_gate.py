"""M6-C live approval gate over the HTTP runtime Internal API (step-less).

SDD it-incident-investigate v1.5 §13.5 / §13.10 (C): the live attempt-pod world
has no Step records (decision D6), so the propose → WAITING_APPROVAL gate, the
surface-bound approve/reject decision, the ITSM Effect closure and the
reject→round-2 revision follow-up must all work at the Run/Unit/Attempt level.
This gate proves, without an LLM:

  1. an ``itsm.handoff`` propose + final checkpoint pause the Run at
     WAITING_APPROVAL (run-level approval record + ApprovalCard surface);
  2. APPROVE executes the durable Effect (fake ITSM connector) and closes the
     Run SUCCEEDED with a terminal Effect + run.terminal outbox;
  3. REJECT reopens the Run to RECOVERING and queues the reviewer revision
     note as a PENDING follow-up (round-2 attempt answers it and re-proposes);
  4. a generic (non-``itsm.handoff``) proposal never pauses the Run (legacy
     record-only behaviour preserved).

Wire discipline mirrors the Pod: every op signs the full bootstrap subject
(including ``execution_unit_id``) and carries fresh lease facts.
"""
from __future__ import annotations

import asyncio
import base64

from fastapi.testclient import TestClient

from enterprise_agent_platform.contracts.enums import (
    ApprovalState,
    AttemptState,
    EffectState,
    ExecutionUnitState,
)
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
    from enterprise_agent_platform import create_app

    return TestClient(create_app(container)), container


def _create_incident_run(client: TestClient, suffix: str) -> str:
    response = client.post(
        "/v1/runs",
        headers={**HEADERS, "Idempotency-Key": f"gate-{suffix}"},
        json={
            "workflow_type": "it-incident-investigate",
            "intent": (
                "Investigate IT incident ticket T20260907 (pay-service 504). "
                "Read the ticket/logs/metrics, write the report to the "
                "/tmp/workspace report, publish it, then propose the ITSM "
                "handoff exactly once and confirm."
            ),
            "resource_refs": [f"incident://ticket/{INCIDENT_DEMO_TICKET_ID}"],
            "host_context_ref": "reference-context:demo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


async def _pre_reserve(container, run_id: str, key: str):
    ctx = RequestContext(
        tenant_id=REFERENCE_LOCAL_TENANT,
        actor_id="gate-test",
        scopes=("runs:execute",),
        request_id=f"pre-reserve-{key}",
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
        transition_key=f"gate-{key}",
    )


def _bootstrap(client: TestClient, container, run_id: str, key: str):
    reservation = asyncio.run(_pre_reserve(container, run_id, key))
    attempt = reservation.attempt
    bootstrap = client.post(
        "/internal/v1/runtime/bootstrap",
        headers={"Authorization": f"Bearer projected:{REFERENCE_LOCAL_TENANT}"},
        json={
            "pod_uid": f"pod-gate-{key}",
            "attempt_id": attempt.attempt_id,
            "generation": attempt.generation,
        },
    )
    assert bootstrap.status_code == 200, bootstrap.text
    body = bootstrap.json()
    unit = asyncio.run(
        container.store.get_primary_unit(REFERENCE_LOCAL_TENANT, run_id)
    )
    base_subject = {
        "tenant_id": REFERENCE_LOCAL_TENANT,
        "run_id": run_id,
        "attempt_id": attempt.attempt_id,
        "generation": attempt.generation,
        "execution_unit_id": unit.execution_unit_id,
    }
    return (
        attempt,
        {"Authorization": f"Bearer {body['runtime_token']}"},
        base_subject,
        {
            **base_subject,
            "lease_owner": body["lease_owner"],
            "lease_version": body["lease_version"],
        },
    )


def _propose_handoff(client: TestClient, headers, subject, action_ref: str) -> None:
    response = client.post(
        "/internal/v1/runtime/action-proposals",
        headers=headers,
        json={
            **subject,
            "action_ref": action_ref,
            "canonical_payload_ref": "x",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted", response.json()


def _commit_final(client: TestClient, headers, subject: dict, summary: str) -> None:
    response = client.post(
        "/internal/v1/runtime/checkpoints/final",
        headers=headers,
        json={
            **subject,
            "summary": summary,
            "agent_state": {"model": "test", "messages": []},
            "agent_state_schema_version": "pi-agent-core/v1",
        },
    )
    assert response.status_code == 200, response.text


def _run_view(client: TestClient, run_id: str) -> dict:
    response = client.get(f"/v1/runs/{run_id}", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["view"]


def _approval_card(client: TestClient, container, run_id: str) -> dict:
    approvals = asyncio.run(
        container.store.list_approval_requests(REFERENCE_LOCAL_TENANT, run_id)
    )
    assert approvals, "no approval records"
    approval_id = approvals[0].approval_id
    surfaces = asyncio.run(
        container.store.list_ui_surfaces(REFERENCE_LOCAL_TENANT, run_id)
    )
    for surface in surfaces:
        response = client.get(
            f"/v1/runs/{run_id}/surfaces/{surface.surface_id}", headers=HEADERS
        )
        assert response.status_code == 200, response.text
        document = response.json()["document"]
        if document.get("component") == "ApprovalCard":
            props = document["props"]
            if props["approval_id"] == approval_id:
                return {
                    "surface_id": surface.surface_id,
                    "revision": response.json()["revision"],
                    **props,
                }
    raise AssertionError("ApprovalCard surface not found")


def _decide(
    client: TestClient,
    run_id: str,
    card: dict,
    decision: str,
    key: str,
) -> None:
    action_ref = card["approve_key" if decision == "APPROVE" else "reject_key"]
    response = client.post(
        f"/v1/runs/{run_id}/actions",
        headers={**HEADERS, "Idempotency-Key": key},
        json={
            "run_id": run_id,
            "surface_id": card["surface_id"],
            "surface_revision": card["revision"],
            "action_ref": action_ref,
            "client_action_id": key,
            "displayed_digest": card["displayed_digest"],
            "host_context_ref": "reference-context:demo",
        },
    )
    assert response.status_code == 202, response.text


def test_approve_handoff_closes_run_with_effect() -> None:
    client, container = _client()
    with client:
        run_id = _create_incident_run(client, "approve")
        attempt, headers, subject, final_subject = _bootstrap(client, container, run_id, "approve")

        # Child publishes the report artifact (content-capable, M6-B READY).
        report = "incident report for T20260907: pay-service 504 root cause"
        published = client.post(
            "/internal/v1/runtime/artifacts",
            headers=headers,
            json={
                "tenant_id": REFERENCE_LOCAL_TENANT,
                "run_id": run_id,
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
                "execution_unit_id": subject["execution_unit_id"],
                "workspace_path": "/tmp/workspace/report-T20260907.md",
                "logical_name": "report-T20260907.md",
                "classification": "report",
                "content_b64": base64.b64encode(report.encode()).decode(),
            },
        )
        assert published.status_code == 200, published.text

        _propose_handoff(client, headers, subject, "itsm.handoff")
        _commit_final(client, headers, final_subject, "Report complete, handoff proposed.")

        view = _run_view(client, run_id)
        assert view["status"] == "WAITING_APPROVAL", view

        card = _approval_card(client, container, run_id)
        _decide(client, run_id, card, "APPROVE", "decision-approve")

        view = _run_view(client, run_id)
        assert view["status"] == "SUCCEEDED", view

        approvals = asyncio.run(
            container.store.list_approval_requests(
                REFERENCE_LOCAL_TENANT, run_id
            )
        )
        assert len(approvals) == 1
        assert approvals[0].status is ApprovalState.APPROVED
        effects = asyncio.run(
            container.store.list_effects(REFERENCE_LOCAL_TENANT, run_id)
        )
        assert len(effects) == 1
        assert effects[0].state is EffectState.SUCCEEDED
        assert effects[0].result_ref and effects[0].result_ref.startswith(
            "reference-defect:"
        )
        unit = asyncio.run(
            container.store.get_primary_unit(REFERENCE_LOCAL_TENANT, run_id)
        )
        assert unit.status is ExecutionUnitState.SUCCEEDED


def test_reject_opens_followup_round_two() -> None:
    client, container = _client()
    with client:
        run_id = _create_incident_run(client, "reject")
        attempt, headers, subject, final_subject = _bootstrap(client, container, run_id, "reject")

        _propose_handoff(client, headers, subject, "itsm.handoff")
        _commit_final(client, headers, final_subject, "Report complete, handoff proposed.")

        view = _run_view(client, run_id)
        assert view["status"] == "WAITING_APPROVAL", view

        card = _approval_card(client, container, run_id)
        _decide(client, run_id, card, "REJECT", "decision-reject")

        view = _run_view(client, run_id)
        assert view["status"] == "RECOVERING", view
        approvals = asyncio.run(
            container.store.list_approval_requests(
                REFERENCE_LOCAL_TENANT, run_id
            )
        )
        assert approvals[0].status is ApprovalState.REJECTED
        followups = asyncio.run(
            container.store.list_followup_requests(
                REFERENCE_LOCAL_TENANT, run_id
            )
        )
        pending = [f for f in followups if f.status == "PENDING"]
        assert len(pending) == 1
        assert "驳回" in pending[0].question and "重新" in pending[0].question
        attempt_record = asyncio.run(
            container.store.get_attempt(
                REFERENCE_LOCAL_TENANT, attempt.attempt_id
            )
        )
        assert attempt_record.status is AttemptState.CHECKPOINTED_FOR_APPROVAL
        # The round-2 attempt is schedulable: unit RECOVERING + no active lease.
        schedulable = asyncio.run(container.store.list_schedulable_work())
        assert any(c.unit.run_id == run_id for c in schedulable)


def test_generic_proposal_does_not_pause() -> None:
    client, container = _client()
    with client:
        run_id = _create_incident_run(client, "generic")
        _attempt, headers, subject, final_subject = _bootstrap(client, container, run_id, "generic")

        _propose_handoff(client, headers, subject, "action:not-registered")
        _commit_final(client, headers, final_subject, "Done.")

        view = _run_view(client, run_id)
        assert view["status"] == "SUCCEEDED", view
        approvals = asyncio.run(
            container.store.list_approval_requests(
                REFERENCE_LOCAL_TENANT, run_id
            )
        )
        assert approvals == ()


def test_decision_idempotent_replay_is_safe() -> None:
    client, container = _client()
    with client:
        run_id = _create_incident_run(client, "replay")
        _attempt, headers, subject, final_subject = _bootstrap(client, container, run_id, "replay")

        _propose_handoff(client, headers, subject, "itsm.handoff")
        _commit_final(client, headers, final_subject, "Report complete, handoff proposed.")
        card = _approval_card(client, container, run_id)
        _decide(client, run_id, card, "APPROVE", "decision-replay")
        _decide(client, run_id, card, "APPROVE", "decision-replay")

        view = _run_view(client, run_id)
        assert view["status"] == "SUCCEEDED", view
        effects = asyncio.run(
            container.store.list_effects(REFERENCE_LOCAL_TENANT, run_id)
        )
        assert len(effects) == 1
        assert effects[0].state is EffectState.SUCCEEDED


def test_run_view_exposes_pending_approval() -> None:
    client, container = _client()
    with client:
        run_id = _create_incident_run(client, "view")
        _attempt, headers, subject, final_subject = _bootstrap(client, container, run_id, "view")
        _propose_handoff(client, headers, subject, "itsm.handoff")
        _commit_final(client, headers, final_subject, "Report complete.")
        view = _run_view(client, run_id)
        assert view["status"] == "WAITING_APPROVAL", view
        # Approval card surface is present and bound to the run-level approval.
        card = _approval_card(client, container, run_id)
        assert card["approval_id"]
