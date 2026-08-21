"""Internal Runtime API is mounted on the main app and drives real transactions.

SDD §13.1 deliverable + Phase 3 predecessor: ``create_internal_router`` is now
part of ``create_agent_platform_app``. These tests exercise the HTTP Runtime
transport path end-to-end against the mounted app:

  1. public POST /v1/runs creates a Run
  2. a scheduler-style ``reserve_attempt`` pre-creates the Attempt (bootstrap needs
     a durable Attempt; the public API does not expose reservation)
  3. POST /internal/v1/runtime/bootstrap (projected host token) issues runtime identity
  4. POST /internal/v1/runtime/artifacts (runtime token) → real Artifact record
  5. POST /internal/v1/runtime/action-proposals (runtime token) → real proposal
     + ARTIFACT_VERSION / ACTION_PROPOSAL events and outbox rows
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from enterprise_agent_platform import create_app
from enterprise_agent_platform.contracts.enums import (
    ActionProposalState,
    ArtifactVersionState,
    EventType,
)
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.reference.local_stack import (
    REFERENCE_LOCAL_BEARER,
    REFERENCE_LOCAL_TENANT,
    create_container,
)

HEADERS = {"Authorization": REFERENCE_LOCAL_BEARER}


def _client():
    container = create_container()
    return TestClient(create_app(container)), container


def _create_run(client: TestClient) -> str:
    response = client.post(
        "/v1/runs",
        headers={**HEADERS, "Idempotency-Key": "internal-api-mount-create"},
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
        actor_id="internal-api-test",
        scopes=("runs:execute",),
        request_id="pre-reserve",
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
        transition_key="internal-api-mount",
    )


def test_internal_bootstrap_and_runtime_ops() -> None:
    client, container = _client()
    with client:
        run_id = _create_run(client)
        reservation = asyncio.run(_pre_reserve(container, run_id))
        attempt = reservation.attempt

        # ── bootstrap (projected host token) ──
        bootstrap = client.post(
            "/internal/v1/runtime/bootstrap",
            headers={"Authorization": f"Bearer projected:{REFERENCE_LOCAL_TENANT}"},
            json={
                "pod_uid": "pod-internal-1",
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
            },
        )
        assert bootstrap.status_code == 200, bootstrap.text
        identity = bootstrap.json()
        assert identity["runtime_token"] == f"runtime-token:{attempt.attempt_id}"
        assert identity["tenant_id"] == REFERENCE_LOCAL_TENANT
        assert identity["attempt_id"] == attempt.attempt_id

        runtime_headers = {"Authorization": f"Bearer {identity['runtime_token']}"}
        subject = {
            "tenant_id": REFERENCE_LOCAL_TENANT,
            "run_id": run_id,
            "attempt_id": attempt.attempt_id,
            "generation": attempt.generation,
        }

        # ── publish artifact via mounted internal API ──
        publish = client.post(
            "/internal/v1/runtime/artifacts",
            headers=runtime_headers,
            json={
                **subject,
                "workspace_path": "work/report.md",
                "logical_name": "report",
                "classification": "analysis",
            },
        )
        assert publish.status_code == 200, publish.text
        artifact_id = publish.json()["result_ref"]

        # ── propose action via mounted internal API ──
        propose = client.post(
            "/internal/v1/runtime/action-proposals",
            headers=runtime_headers,
            json={**subject, "action_ref": "act:notify", "canonical_payload_ref": "work/notice.json"},
        )
        assert propose.status_code == 200, propose.text

        # ── durable effects: artifact + proposal records, events, outbox ──
        async def _verify() -> None:
            version = await container.store.get_artifact_version(
                REFERENCE_LOCAL_TENANT, artifact_id, 1
            )
            assert version.state is ArtifactVersionState.STAGING
            proposal = await container.store.get_action_proposal(
                REFERENCE_LOCAL_TENANT, "act:notify"
            )
            assert proposal.status is ActionProposalState.OPEN
            events = await container.store.list_events(REFERENCE_LOCAL_TENANT, run_id)
            kinds = {e.event_type for e in events}
            assert EventType.ARTIFACT_VERSION in kinds
            assert EventType.ACTION_PROPOSAL in kinds
            outbox = await container.store.list_outbox(REFERENCE_LOCAL_TENANT)
            topics = {m.topic for m in outbox}
            assert "artifact.prepared" in topics
            assert "action.proposed" in topics

        asyncio.run(_verify())


def test_internal_bootstrap_rejects_bad_projection() -> None:
    client, _container = _client()
    with client:
        response = client.post(
            "/internal/v1/runtime/bootstrap",
            headers={"Authorization": "Bearer bogus"},
            json={"pod_uid": "pod-x", "attempt_id": "nope", "generation": 1},
        )
        assert response.status_code == 401, response.text