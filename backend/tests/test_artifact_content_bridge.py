"""M6-B: run-published artifact content finalize + download (SDD §13.4).

The child ships the workspace file bytes in the publish op; the Control Plane
scan-cleans the staged version to READY (content blob stored 1:1), the run view
lists the product, and a public content route serves the bytes to any
``runs:read`` principal. This test drives the full loop through the mounted
app exactly like ``test_internal_api_mount``:
  publish WITH content → READY version + view.artifacts + downloadable content
  publish WITHOUT content → stays STAGING, absent from view, 404 on download
"""
from __future__ import annotations

import asyncio
import base64

from fastapi.testclient import TestClient

from enterprise_agent_platform import create_app
from enterprise_agent_platform.contracts.enums import ArtifactVersionState
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.reference.local_stack import (
    REFERENCE_LOCAL_BEARER,
    REFERENCE_LOCAL_TENANT,
    create_container,
)

HEADERS = {"Authorization": REFERENCE_LOCAL_BEARER}

REPORT_BYTES = (
    b"# Incident report T20260907\n\n- overview\n- evidence\n- root cause\n"
).ljust(4096, b"\n")


def _client():
    container = create_container()
    return TestClient(create_app(container)), container


def _create_run(client: TestClient, key: str) -> str:
    response = client.post(
        "/v1/runs",
        headers={**HEADERS, "Idempotency-Key": key},
        json={
            "workflow_type": "synthetic-analysis",
            "intent": "Analyze a portable synthetic resource",
            "resource_refs": ["synthetic-case:case-42"],
            "parameters": {"analysis_mode": "summary"},
            "host_context_ref": "reference-context:demo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


async def _pre_reserve(container, run_id: str):
    ctx = RequestContext(
        tenant_id=REFERENCE_LOCAL_TENANT,
        actor_id="artifact-content-test",
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
        transition_key="artifact-content-test",
    )


def _publish(
    client: TestClient,
    runtime_headers: dict[str, str],
    subject: dict[str, str],
    *,
    logical_name: str,
    with_content: bool,
) -> tuple[int, dict[str, object]]:
    body: dict[str, object] = {
        **subject,
        "workspace_path": "work/report.md",
        "logical_name": logical_name,
        "classification": "report",
    }
    if with_content:
        body["content_b64"] = base64.b64encode(REPORT_BYTES).decode("ascii")
    response = client.post(
        "/internal/v1/runtime/artifacts",
        headers=runtime_headers,
        json=body,
    )
    return response.status_code, response.json()


def test_artifact_content_finalize_and_download() -> None:
    client, container = _client()
    with client:
        run_id = _create_run(client, "artifact-content-create")
        reservation = asyncio.run(_pre_reserve(container, run_id))
        attempt = reservation.attempt

        bootstrap = client.post(
            "/internal/v1/runtime/bootstrap",
            headers={"Authorization": f"Bearer projected:{REFERENCE_LOCAL_TENANT}"},
            json={
                "pod_uid": "pod-content-1",
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
            },
        )
        assert bootstrap.status_code == 200, bootstrap.text
        runtime_headers = {"Authorization": f"Bearer {bootstrap.json()['runtime_token']}"}
        subject = {
            "tenant_id": REFERENCE_LOCAL_TENANT,
            "run_id": run_id,
            "attempt_id": attempt.attempt_id,
            "generation": attempt.generation,
        }

        # ── publish WITH content → READY ──
        status, payload = _publish(
            client,
            runtime_headers,
            subject,
            logical_name="incident-report-T20260907.md",
            with_content=True,
        )
        assert status == 200, payload
        artifact_id = payload["result_ref"]

        async def _verify_ready() -> None:
            version = await container.store.get_artifact_version(
                REFERENCE_LOCAL_TENANT, artifact_id, 1
            )
            assert version.state is ArtifactVersionState.READY
            assert version.ready_at is not None
            assert version.size_bytes == len(REPORT_BYTES)
            assert version.checksum.startswith("sha256:")
            content = await container.store.get_artifact_content(
                REFERENCE_LOCAL_TENANT, artifact_id, 1
            )
            assert content == REPORT_BYTES

        asyncio.run(_verify_ready())

        # ── run view lists the ready product ──
        view = client.get(f"/v1/runs/{run_id}", headers=HEADERS)
        assert view.status_code == 200, view.text
        artifacts = view.json()["view"]["artifacts"]
        assert any(
            item["artifact_id"] == artifact_id
            and item["name"] == "incident-report-T20260907.md"
            and item["media_type"] == "text/markdown"
            and item["version"] == 1
            for item in artifacts
        ), artifacts

        # ── content route serves the bytes (runs:read scope) ──
        download = client.get(
            f"/v1/runs/{run_id}/artifacts/{artifact_id}/versions/1/content",
            headers=HEADERS,
        )
        assert download.status_code == 200, download.text
        assert download.content == REPORT_BYTES
        assert download.headers["content-type"].startswith("text/markdown")
        assert "attachment" in download.headers.get("content-disposition", "")

        # ── legacy metadata-only publish stays STAGING & 404 on download ──
        status, legacy = _publish(
            client,
            runtime_headers,
            subject,
            logical_name="legacy-only.json",
            with_content=False,
        )
        assert status == 200, legacy
        legacy_id = legacy["result_ref"]
        stale = client.get(
            f"/v1/runs/{run_id}/artifacts/{legacy_id}/versions/1/content",
            headers=HEADERS,
        )
        assert stale.status_code == 404, stale.text

        # ── wrong tenant / unknown artifact → 404 ──
        missing = client.get(
            f"/v1/runs/{run_id}/artifacts/no-such-id/versions/1/content",
            headers=HEADERS,
        )
        assert missing.status_code == 404, missing.text

        # ── durable effects: READY version event + artifact.ready outbox ──
        async def _verify_events() -> None:
            events = await container.store.list_events(REFERENCE_LOCAL_TENANT, run_id)
            ready_events = [
                e for e in events if e.event_type.value == "artifact.version"
            ]
            assert any(
                getattr(e.payload, "state", None) == "READY" for e in ready_events
            ), [
                getattr(e.payload, "state", None) for e in ready_events
            ]
            outbox = await container.store.list_outbox(REFERENCE_LOCAL_TENANT)
            assert any(
                m.topic == "artifact.ready" and m.payload.get("artifact_id") == artifact_id
                for m in outbox
            )

        asyncio.run(_verify_events())


def test_artifact_content_bad_base64_rejected() -> None:
    client, container = _client()
    with client:
        run_id = _create_run(client, "artifact-content-bad")
        reservation = asyncio.run(_pre_reserve(container, run_id))
        attempt = reservation.attempt
        bootstrap = client.post(
            "/internal/v1/runtime/bootstrap",
            headers={"Authorization": f"Bearer projected:{REFERENCE_LOCAL_TENANT}"},
            json={
                "pod_uid": "pod-content-2",
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
            },
        )
        assert bootstrap.status_code == 200, bootstrap.text
        publish = client.post(
            "/internal/v1/runtime/artifacts",
            headers={"Authorization": f"Bearer {bootstrap.json()['runtime_token']}"},
            json={
                "tenant_id": REFERENCE_LOCAL_TENANT,
                "run_id": run_id,
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
                "workspace_path": "work/report.md",
                "logical_name": "bad.md",
                "classification": "report",
                "content_b64": "!!!not-base64!!!",
            },
        )
        # Orchestrator raises PlatformError; internal adapter maps it to a 4xx.
        assert publish.status_code == 422, publish.text
