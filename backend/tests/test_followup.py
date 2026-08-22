"""Phase 1 unit tests: the follow-up routing endpoint."""
from __future__ import annotations

import asyncio
from dataclasses import replace

from fastapi.testclient import TestClient

from enterprise_agent_platform import create_app
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.reference.local_stack import (
    REFERENCE_LOCAL_BEARER,
    REFERENCE_LOCAL_TENANT,
    create_container,
)

HEADERS = {"Authorization": REFERENCE_LOCAL_BEARER}


def _client() -> TestClient:
    return TestClient(create_app(create_container()))


def _client_container() -> tuple[TestClient, object]:
    container = create_container()
    return TestClient(create_app(container)), container


def _create_run(client: TestClient) -> str:
    response = client.post(
        "/v1/runs",
        headers={**HEADERS, "Idempotency-Key": "followup-create-run"},
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


def _followup(client: TestClient, run_id: str, key: str, question: str) -> object:
    return client.post(
        f"/v1/runs/{run_id}/followups",
        headers={**HEADERS, "Idempotency-Key": key},
        json={"run_id": run_id, "question": question, "client_followup_id": key},
    )


def test_followup_returns_answer() -> None:
    client = _client()
    run_id = _create_run(client)
    response = _followup(client, run_id, "followup-1", "为什么是这个结果？")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "followup-answer/v1"
    assert body["run_id"] == run_id
    assert body["session_id"]
    assert body["answer"]


def test_followup_is_idempotent() -> None:
    client = _client()
    run_id = _create_run(client)
    first = _followup(client, run_id, "followup-1", "为什么？")
    second = _followup(client, run_id, "followup-1", "为什么？")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["answer"] == second.json()["answer"]


def test_followup_does_not_write_run_events() -> None:
    client = _client()
    run_id = _create_run(client)
    before = client.get(f"/v1/runs/{run_id}/events", headers=HEADERS).json()["watermark"]
    response = _followup(client, run_id, "followup-1", "结果依据是什么？")
    assert response.status_code == 200
    after = client.get(f"/v1/runs/{run_id}/events", headers=HEADERS).json()["watermark"]
    assert before == after


def test_followup_unknown_run_returns_404() -> None:
    client = _client()
    response = _followup(client, "missing", "followup-1", "你好")
    assert response.status_code == 404


def test_followup_identity_mismatch_rejected() -> None:
    client = _client()
    run_id = _create_run(client)
    response = client.post(
        f"/v1/runs/{run_id}/followups",
        headers={**HEADERS, "Idempotency-Key": "followup-1"},
        json={"run_id": run_id, "question": "你好", "client_followup_id": "followup-OTHER"},
    )
    assert response.status_code == 422


def test_followup_history_aggregates_pending_answered_and_inline() -> None:
    """GET /followups must surface the durable history (SDD §13.1 gap).

    History aggregation includes: durable PENDING rows (queued for a fresh
    Attempt, no answer yet), durable ANSWERED rows (orchestrator wrote the
    answer back), and inline in-process answers — deduped by
    ``client_followup_id`` so the scheduling path's cache never doubles a
    durable record.
    """
    client, container = _client_container()
    with client:
        run_id = _create_run(client)
        ctx = RequestContext(
            tenant_id=REFERENCE_LOCAL_TENANT,
            actor_id="followup-history-test",
            scopes=("runs:execute", "runs:write"),
            request_id="followup-history",
        )

        # 1. durable PENDING — queued for a fresh Attempt (scheduler path).
        pending = asyncio.run(
            container.control.queue_followup(
                ctx,
                run_id,
                question="Queued question?",
                client_followup_id="durable-pending",
            )
        )
        assert pending.status == "PENDING"

        # 2. durable ANSWERED — queued then answered by the orchestrator CAS.
        answered = asyncio.run(
            container.control.queue_followup(
                ctx,
                run_id,
                question="Answered question?",
                client_followup_id="durable-answered",
            )
        )

        async def _answer(followup_id: str, answer: str) -> None:
            async with container.store.transaction() as tx:
                current = await tx.get_followup_request(REFERENCE_LOCAL_TENANT, followup_id)
                now = await tx.db_now()
                done = replace(
                    current,
                    status="ANSWERED",
                    answer=answer,
                    answered_at=now,
                    version=current.version + 1,
                )
                await tx.replace_followup_request_cas(done, current.version)

        asyncio.run(_answer(answered.followup_id, "The durable answer."))

        # 3. inline answer — live-run path, cached in-process only.
        inline = client.post(
            f"/v1/runs/{run_id}/followups",
            headers={**HEADERS, "Idempotency-Key": "inline-1"},
            json={
                "run_id": run_id,
                "question": "Inline question?",
                "client_followup_id": "inline-1",
            },
        )
        assert inline.status_code == 200, inline.text

        # 4. history aggregates all three, deduped and ordered by time.
        history = client.get(f"/v1/runs/{run_id}/followups", headers=HEADERS).json()
        assert history["total_count"] == 3, history
        recorded = history["records"]
        by_cfu = {item["client_followup_id"]: item for item in recorded}
        assert set(by_cfu) == {"durable-pending", "durable-answered", "inline-1"}, recorded
        assert by_cfu["durable-pending"]["status"] == "PENDING"
        assert by_cfu["durable-pending"]["answer"] is None
        assert by_cfu["durable-pending"]["answered_at"] is None
        assert by_cfu["durable-answered"]["status"] == "ANSWERED"
        assert by_cfu["durable-answered"]["answer"] == "The durable answer."
        assert by_cfu["durable-answered"]["answered_at"]
        assert by_cfu["inline-1"]["status"] == "ANSWERED"
        assert by_cfu["inline-1"]["answer"]
        # Timeline ordering: pending (created first) → durable answer → inline.
        assert [item["client_followup_id"] for item in recorded] == [
            "durable-pending",
            "durable-answered",
            "inline-1",
        ], recorded
        # Followup seq is an ordered index.
        assert [item["followup_seq"] for item in recorded] == [0, 1, 2]


def test_followup_history_unknown_run_returns_404() -> None:
    client = _client()
    response = client.get("/v1/runs/missing/followups", headers=HEADERS)
    assert response.status_code == 404
