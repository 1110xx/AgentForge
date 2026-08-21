"""Phase 1 unit tests: the follow-up routing endpoint."""
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
