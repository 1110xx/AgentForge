""""Phase 3.6 unit tests: the free-form chat endpoint (frontend launcher)."""
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


def _chat(
    client: TestClient,
    message: str,
    key: str,
    *,
    workflow_hint: str | None = None,
    headers: dict[str, str] | None = None,
):
    body: dict[str, object] = {"message": message}
    if workflow_hint is not None:
        body["workflow_hint"] = workflow_hint
    return client.post(
        "/v1/chat",
        headers={**HEADERS, "Idempotency-Key": key, **(headers or {})},
        json=body,
    )


def test_chat_creates_run_and_returns_location() -> None:
    client = _client()
    response = _chat(client, "分析日志中的故障模式", "chat-create-1")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["schema_version"] == "run-view-snapshot/v1"
    assert body["run_id"]
    assert response.headers["Location"] == f"/v1/runs/{body['run_id']}"


def test_chat_requires_auth() -> None:
    client = _client()
    response = client.post(
        "/v1/chat",
        headers={"Idempotency-Key": "chat-no-auth"},
        json={"message": "hello"},
    )
    assert response.status_code == 401, response.text


def test_chat_requires_idempotency_key() -> None:
    client = _client()
    response = client.post(
        "/v1/chat",
        headers=HEADERS,
        json={"message": "hello"},
    )
    assert response.status_code == 422, response.text


def test_chat_rejects_empty_message() -> None:
    client = _client()
    response = _chat(client, "", "chat-empty")
    assert response.status_code == 422, response.text
    response = _chat(client, "   ", "chat-blank")
    assert response.status_code == 422, response.text


def test_chat_intent_mapping_picks_workflow_and_keeps_intent() -> None:
    client = _client()
    response = _chat(client, "分析日志中的故障模式", "chat-intent-1")
    assert response.status_code == 201, response.text
    view = response.json()["view"]
    assert view["workflow_type"] == "synthetic-analysis"
    assert view["intent"] == "分析日志中的故障模式"


def test_chat_unmatched_message_falls_back_to_default_workflow() -> None:
    client = _client()
    response = _chat(client, "帮我研究一下这个项目", "chat-intent-2")
    assert response.status_code == 201, response.text
    assert response.json()["view"]["intent"] == "帮我研究一下这个项目"


def test_chat_workflow_hint_wins() -> None:
    client = _client()
    response = _chat(
        client,
        "whatever",
        "chat-hint-1",
        workflow_hint="synthetic-analysis",
    )
    assert response.status_code == 201, response.text
    assert response.json()["view"]["workflow_type"] == "synthetic-analysis"


def test_chat_is_idempotent_same_key_same_body() -> None:
    client = _client()
    first = _chat(client, "分析故障趋势", "chat-idem-1")
    second = _chat(client, "分析故障趋势", "chat-idem-1")
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["run_id"] == second.json()["run_id"]


def test_chat_creates_run_that_accepts_followups() -> None:
    client = _client()
    created = _chat(client, "分析日志", "chat-followup-1")
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    followup = client.post(
        f"/v1/runs/{run_id}/followups",
        headers={**HEADERS, "Idempotency-Key": "chat-followup-q1"},
        json={"run_id": run_id, "question": "为什么？", "client_followup_id": "chat-followup-q1"},
    )
    assert followup.status_code == 200, followup.text
    assert followup.json()["schema_version"] == "followup-answer/v1"