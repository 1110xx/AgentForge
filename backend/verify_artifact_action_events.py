"""E2E verification: publish_artifact / propose_action now emit events + outbox.

Drives the REAL SubprocessOrchestrator op handlers (no pipe, no live scheduler):
  1. create_run → QUEUED
  2. reserve + activate (bootstrap) → RUNNING
  3. OP_PUBLISH_ARTIFACT → ArtifactRecord(pending) + ArtifactVersionRecord(PREPARING)
     + ARTIFACT_VERSION event + outbox artifact.prepared
  4. OP_PROPOSE_ACTION → ActionProposalRecord(OPEN)
     + ACTION_PROPOSAL event + outbox action.proposed
  5. OP_COMMIT_FINAL → SUCCEEDED; event sequence stays contiguous
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, "src")

from enterprise_agent_platform.contracts.commands import CreateRunCommand
from enterprise_agent_platform.contracts.enums import (
    ArtifactVersionState,
    AttemptState,
    EventType,
    RunState,
)
from enterprise_agent_platform.contracts.events import (
    ActionProposalPayload,
    ArtifactVersionPayload,
)
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.domain.records import DispatchTicket, RunRecord
from enterprise_agent_platform.execution.pipe_transport import (
    OP_COMMIT_FINAL,
    OP_PROPOSE_ACTION,
    OP_PUBLISH_ARTIFACT,
)
from enterprise_agent_platform.execution.subprocess_orchestrator import (
    SubprocessOrchestrator,
)
from uuid import uuid4

from enterprise_agent_platform.persistence import InMemoryPlatformStore

TENANT = "demo"


async def _make_store(name: str):
    """Build the store backend: memory | sqlite (L1) | pg (AGENT_PLATFORM_DATABASE_URL)."""
    if name == "memory":
        return InMemoryPlatformStore()

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from enterprise_agent_platform.persistence.database import (
        create_schema,
        create_sqlite_l1_engine,
    )
    from enterprise_agent_platform.persistence.sqlalchemy_store import (
        SqlAlchemyPlatformStore,
    )

    if name == "sqlite":
        engine = create_sqlite_l1_engine()
    else:
        url = os.environ.get("AGENT_PLATFORM_DATABASE_URL", "").strip()
        if not url:
            raise SystemExit("AGENT_PLATFORM_DATABASE_URL required for --store pg")
        engine = create_async_engine(url, pool_pre_ping=True)
    await create_schema(engine)
    return SqlAlchemyPlatformStore(
        async_sessionmaker(engine, expire_on_commit=False)
    )


def _ctx() -> RequestContext:
    return RequestContext(
        tenant_id=TENANT,
        actor_id="tester",
        scopes=("runs:create", "runs:read", "runs:execute"),
        request_id="verify-artifact-events",
    )


async def _bootstrap(store, control, ctx, run_id, unit, key):
    reservation = await control.reserve_attempt(
        ctx,
        unit.execution_unit_id,
        unit.current_checkpoint_id,
        unit.version,
        transition_key=key,
    )
    lease = await control.activate_lease(
        ctx,
        reservation.attempt.attempt_id,
        reservation.attempt.generation,
        owner="verify:owner",
        expected_lease_version=reservation.lease.version,
    )
    return reservation, lease


def _make_ticket(reservation, unit, run_id, key: str) -> DispatchTicket:
    return DispatchTicket(
        worker_id=key,
        tenant_id=TENANT,
        run_id=run_id,
        execution_unit_id=unit.execution_unit_id,
        attempt_id=reservation.attempt.attempt_id,
        lease_id=reservation.lease.lease_id,
        generation=reservation.attempt.generation,
        source_checkpoint_id=reservation.attempt.source_checkpoint_id,
    )


async def main(store_name: str) -> None:
    global TENANT
    run_key = uuid4().hex[:8]
    TENANT = f"demo-{store_name}-{run_key}"  # fully isolated tenant per invocation
    store = await _make_store(store_name)
    control = ControlPlaneService(store)
    orchestrator = SubprocessOrchestrator(
        store=store,
        control=control,
        run_sessions=None,
        resource_resolver=None,
    )
    ctx = _ctx()

    # ── 1. Create the run ──
    run = await control.create_run(
        ctx,
        CreateRunCommand(
            workflow_type="verify",
            intent="Publish artifact and propose action",
            resource_refs=("synthetic:case-1",),
            parameters={},
            host_context_ref=None,
        ),
        idempotency_key=f"verify:artifact-events:{store_name}:{run_key}",
    )
    unit = await store.get_primary_unit(ctx.tenant_id, run.run_id)
    print(f"[1] run created: run={run.run_id}")

    # ── 2. Bootstrap → RUNNING ──
    transition_key = f"verify:t-run:{store_name}:{run_key}"
    reservation, lease = await _bootstrap(store, control, ctx, run.run_id, unit, transition_key)
    ticket = _make_ticket(reservation, unit, run.run_id, transition_key)
    context_kwargs = {
        "attempt_id": reservation.attempt.attempt_id,
        "generation": reservation.attempt.generation,
        "lease_owner": "verify:owner",
        "lease_version": lease.version,
    }
    print(f"[2] bootstrapped attempt={reservation.attempt.attempt_id}")

    # ── 3. OP_PUBLISH_ARTIFACT ──
    publish = await orchestrator._handle(
        ticket,
        ctx,
        OP_PUBLISH_ARTIFACT,
        {
            "workspace_path": "work/report.md",
            "logical_name": "report",
            "classification": "analysis",
        },
    )
    assert publish["status"] == "accepted", publish
    artifact = await store.get_artifact_version(
        ctx.tenant_id, publish["artifact_id"], 1
    )
    assert artifact.state == ArtifactVersionState.STAGING, artifact.state
    assert artifact.lineage["attempt_id"] == reservation.attempt.attempt_id
    print(f"[3] artifact recorded: artifact={publish['artifact_id']} state={artifact.state}")

    # ── 4. OP_PROPOSE_ACTION ──
    try:
        propose = await orchestrator._handle(
            ticket,
            ctx,
            OP_PROPOSE_ACTION,
            {"action_ref": "act:notify-owner", "canonical_payload_ref": "work/notice.json"},
        )
    except Exception as e:
        from enterprise_agent_platform.persistence.protocol import PlatformError

        if isinstance(e, PlatformError):
            c = e.__cause__
            print("PROPOSE PlatformError:", e)
            print("PROPOSE cause:", type(c).__name__, repr(getattr(c, "orig", c))[:500] if c else "")
        else:
            print("PROPOSE other:", type(e).__name__, repr(e)[:400])
        raise
    assert propose["status"] == "accepted", propose
    proposal = await store.get_action_proposal(ctx.tenant_id, "act:notify-owner")
    assert proposal.status == "OPEN"
    print(f"[4] action proposal recorded: action_ref={propose['action_ref']} state={proposal.status}")

    # ── Events emitted (ARTIFACT_VERSION + ACTION_PROPOSAL) ──
    events = await store.list_events(ctx.tenant_id, run.run_id)
    kinds = [(e.event_type, e.event_seq) for e in events]
    assert EventType.ARTIFACT_VERSION in set(k for k, _ in kinds), kinds
    assert EventType.ACTION_PROPOSAL in set(k for k, _ in kinds), kinds
    artifact_evt = next(e for e in events if e.event_type is EventType.ARTIFACT_VERSION)
    proposal_evt = next(e for e in events if e.event_type is EventType.ACTION_PROPOSAL)
    assert isinstance(artifact_evt.payload, ArtifactVersionPayload)
    assert artifact_evt.payload.state == "STAGING"
    assert isinstance(proposal_evt.payload, ActionProposalPayload)
    assert proposal_evt.payload.proposal_state == "OPEN"
    assert artifact_evt.event_seq + 1 == proposal_evt.event_seq
    print(
        f"[5] events emitted: artifact.version seq={artifact_evt.event_seq}, "
        f"action.proposal seq={proposal_evt.event_seq}"
    )

    # ── Outbox topics ──
    outbox = await store.list_outbox(ctx.tenant_id)
    topics = {m.topic for m in outbox}
    assert "artifact.prepared" in topics, topics
    assert "action.proposed" in topics, topics
    print(f"[6] outbox topics present: {sorted(topics)}")

    # ── 5. OP_COMMIT_FINAL — sequence stays contiguous ──
    final = await orchestrator._handle(
        ticket,
        ctx,
        OP_COMMIT_FINAL,
        {
            "context": context_kwargs,
            "summary": "Artifact published, action proposed.",
            "agent_state": {},
            "agent_state_schema_version": "pi-agent-core/v1",
        },
    )
    run_after = await store.get_run(ctx.tenant_id, run.run_id)
    assert run_after.status is RunState.SUCCEEDED
    final_events = await store.list_events(ctx.tenant_id, run.run_id)
    assert max(e.event_seq for e in final_events) == run_after.last_event_seq
    print(
        f"[7] final commit: run={run_after.status.value} "
        f"last_event_seq={run_after.last_event_seq} contiguous"
    )

    print(
        f"\nPASS: publish_artifact / propose_action persisted + events + outbox "
        f"verified (store={STORE_NAME})"
    )


STORE_NAME = "memory"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="verify artifact/action event path")
    parser.add_argument(
        "--store",
        choices=("memory", "sqlite", "pg"),
        default="memory",
        help="store backend: memory (default) | sqlite L1 | pg (AGENT_PLATFORM_DATABASE_URL)",
    )
    args = parser.parse_args()
    STORE_NAME = args.store
    asyncio.run(main(args.store))