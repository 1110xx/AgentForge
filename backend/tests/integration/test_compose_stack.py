"""L2 disposable Compose gate: durable PostgreSQL, NATS and MinIO integration.

Runs inside the disposable stack created by scripts/test-compose.sh. The gate is
deliberately small and honest: it proves that the control-plane services persist
and replay facts on a real PostgreSQL instance, that the NATS endpoint answers,
and that the versioned artifact bucket is provisioned by minio-init.

It must never claim durability it did not exercise, so each check talks to a
real service over the network, not to an in-process fake.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from enterprise_agent_platform import create_in_memory_container
from enterprise_agent_platform.contracts.commands import CreateRunCommand
from enterprise_agent_platform.contracts.enums import EffectState, EventType, RunState
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.views import RunQueryService
from enterprise_agent_platform.integration.host import resolve_run_authorization
from enterprise_agent_platform.persistence.sqlalchemy_store import SqlAlchemyPlatformStore
from enterprise_agent_platform.reference.local_stack import (
    _ReferenceAuth,
    _ReferenceHostContext,
    _ReferencePolicy,
    _ReferenceResources,
)
from enterprise_agent_platform.reference.provider import ReferenceWorkflowHarness

EXPECTED_TABLES = {
    "run",
    "run_authorization_snapshot",
    "step",
    "execution_unit",
    "checkpoint",
    "attempt",
    "attempt_step",
    "execution_lease",
    "workspace_snapshot",
    "artifact",
    "artifact_version",
    "tool_grant",
    "tool_invocation",
    "action_proposal",
    "approval_request",
    "effect_ledger",
    "ui_surface",
    "ui_surface_revision",
    "run_event",
    "audit_event",
    "outbox_message",
    "inbox_message",
    "cost_ledger",
    "execution_error",
    "idempotency_record",
}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _database_url() -> str:
    url = _env("AGENT_PLATFORM_L2_DATABASE_URL") or _env("AGENT_PLATFORM_DATABASE_URL")
    if not url:
        pytest.skip("AGENT_PLATFORM_L2_DATABASE_URL is not set (L2 stack not running)")
    return url


def _run(coro) -> object:
    return asyncio.run(coro)


def _make_container(store: SqlAlchemyPlatformStore):
    return create_in_memory_container(
        auth_context_provider=_ReferenceAuth(),
        resource_resolver=_ReferenceResources(),
        host_context_verifier=_ReferenceHostContext(),
        policy_context_provider=_ReferencePolicy(),
        store=store,
    )


def test_migrations_applied() -> None:
    url = _database_url()

    async def _tables() -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                        )
                    )
                ).scalars().all()
        finally:
            await engine.dispose()
        return set(rows)

    tables = _run(_tables())
    missing = EXPECTED_TABLES - tables
    assert not missing, f"migration did not create tables: {sorted(missing)}"


def test_durable_create_run_round_trip() -> None:
    url = _database_url()

    async def scenario() -> None:
        engine = create_async_engine(url)
        try:
            store = SqlAlchemyPlatformStore(
                async_sessionmaker(engine, expire_on_commit=False)
            )
            container = _make_container(store)

            ctx = RequestContext(
                tenant_id="reference-local",
                actor_id="l2-gate",
                scopes=("runs:create", "runs:read"),
                request_id="l2-request-1",
                trace_id="l2-trace-1",
            )
            command = CreateRunCommand(
                workflow_type="synthetic-analysis",
                intent="L2 durable gate",
                resource_refs=("synthetic-case:case-42",),
                parameters={"max_items": 10},
                host_context_ref="reference-context:l2",
            )
            authority = await resolve_run_authorization(
                ctx,
                command,
                resource_resolver=container.resource_resolver,
                host_context_verifier=container.host_context_verifier,
                policy_context_provider=container.policy_context_provider,
                timeout_seconds=5.0,
            )
            run = await container.control.create_run(
                ctx, command, "l2-durable-1", authorization=authority
            )
            run_id = run.run_id

            # A brand-new store proves the facts live in PostgreSQL, not in memory.
            fresh = _make_container(
                SqlAlchemyPlatformStore(
                    async_sessionmaker(engine, expire_on_commit=False)
                )
            )
            query = RunQueryService(fresh.store)
            snapshot = await query.get_snapshot(ctx.tenant_id, run_id)
            assert snapshot.status == RunState.QUEUED
            assert snapshot.view.run_id == run_id

            page = await query.get_events(
                ctx.tenant_id, run_id, after_event_seq=0, limit=100
            )
            event_types = [event.event_type for event in page.events]
            assert EventType.RUN_CREATED in event_types

            # Raw SQL proof: the authorization snapshot row carries policy scopes.
            async with engine.connect() as conn:
                scopes = (
                    await conn.execute(
                        text(
                            "SELECT policy_scopes FROM run_authorization_snapshot"
                            " WHERE run_id = :run_id"
                        ),
                        {"run_id": run_id},
                    )
                ).scalar_one()
            assert "synthetic:read" in scopes
        finally:
            await engine.dispose()

    _run(scenario())


def test_reference_vertical_on_postgres() -> None:
    """Full reference vertical on real PostgreSQL: approve -> Effect -> finish.

    This is the durable twin of wheel-smoke's in-memory vertical. Every fact is
    written to PostgreSQL and re-read by a brand-new store, so the test proves
    the whole control-plane chain (tool grant, surfaces, approval decision,
    Effect ledger, terminal events) survives a process restart.
    """

    url = _database_url()

    async def scenario() -> None:
        engine = create_async_engine(url)
        try:
            store = SqlAlchemyPlatformStore(
                async_sessionmaker(engine, expire_on_commit=False)
            )
            harness = ReferenceWorkflowHarness(store=store)
            paused = await harness.run_to_approval()
            completed = await harness.approve_and_complete(
                paused,
                actor_id="l2-reviewer",
                client_action_id="l2-approve-1",
            )
            assert completed.run.status is RunState.SUCCEEDED
            assert completed.effect.state is EffectState.SUCCEEDED
            assert completed.effect.remote_operation_id is not None
            assert harness.fake_connector.create_count == 1

            tenant_id = paused.context.tenant_id
            run_id = paused.run.run_id

            # Durability: a brand-new store replays the whole vertical from PG.
            fresh = SqlAlchemyPlatformStore(
                async_sessionmaker(engine, expire_on_commit=False)
            )
            query = RunQueryService(fresh)
            snapshot = await query.get_snapshot(tenant_id, run_id)
            assert snapshot.status is RunState.SUCCEEDED
            page = await query.get_events(
                tenant_id, run_id, after_event_seq=0, limit=200
            )
            kinds = [event.payload.kind for event in page.events]
            for expected in (
                "approval.decided",
                "effect.status.changed",
                "ui.surface.committed",
                "run.status.changed",
            ):
                assert expected in kinds, f"missing event {expected} in {kinds}"

            effects = await fresh.list_effects(tenant_id, run_id)
            assert len(effects) == 1
            assert effects[0].state is EffectState.SUCCEEDED
            assert effects[0].result_ref is not None

            surfaces = await fresh.list_ui_surfaces(tenant_id, run_id)
            assert len(surfaces) == 3
            revisions = await fresh.get_ui_surface_revision(
                tenant_id,
                f"approval-{run_id}",
                1,
            )
            assert revisions.document["component"] == "ApprovalCard"
        finally:
            await engine.dispose()

    _run(scenario())


def test_nats_reachable() -> None:
    url = _env("AGENT_PLATFORM_L2_NATS_URL")
    if not url:
        pytest.skip("AGENT_PLATFORM_L2_NATS_URL is not set (L2 stack not running)")

    async def ping() -> None:
        import nats

        client = await nats.connect(
            url, connect_timeout=5, max_reconnect_attempts=0
        )
        try:
            await client.flush(timeout=2)
            assert client.is_connected
        finally:
            await client.close()

    _run(ping())


def test_minio_versioned_bucket_exists() -> None:
    endpoint = _env("AGENT_PLATFORM_L2_S3_ENDPOINT") or "http://minio:9000"
    access_key = (
        _env("AGENT_PLATFORM_L2_S3_ACCESS_KEY_ID")
        or _env("AGENT_PLATFORM_S3_ACCESS_KEY_ID")
    )
    secret_key = (
        _env("AGENT_PLATFORM_S3_SECRET_ACCESS_KEY") or _env("MINIO_ROOT_PASSWORD")
    )
    bucket = _env("AGENT_PLATFORM_S3_BUCKET") or "agent-artifacts"
    if not (access_key and secret_key):
        pytest.skip("S3 credentials are not set")

    import boto3
    from botocore.config import Config as BotoConfig

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=BotoConfig(signature_version="s3v4"),
    )
    buckets = {entry["Name"] for entry in client.list_buckets()["Buckets"]}
    assert bucket in buckets, f"bucket {bucket} was not provisioned by minio-init"

    versioning = client.get_bucket_versioning(Bucket=bucket)
    assert versioning.get("Status") == "Enabled", "bucket versioning was not enabled"
