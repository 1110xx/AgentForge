"""End-to-end verification: Checkpoint chain with pi-agent-core Agent snapshots.

Drives the REAL SubprocessOrchestrator op handlers (no pipe, no live scheduler):
  1. create_run → QUEUED + initial COMMITTED checkpoint (agent_state={})
  2. reserve + activate (bootstrap)
  3. OP_COMMIT_CHECKPOINT (mid-run turn end) → checkpoint_seq=1, agent_state persisted
  4. OP_COMMIT_FINAL → checkpoint_seq=2 (final Agent snapshot) + Run SUCCEEDED
  5. Follow-up on terminal Run → Run/Unit RECOVERING + PENDING FollowupRequestRecord
  6. Scheduler claims the new Attempt
  7. OP_RESTORE → restore carries final agent_state + followup_question (history rehydratable)
  8. New Attempt OP_COMMIT_FINAL (answer) → checkpoint_seq=3, answer written back
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import replace
from uuid import uuid4

sys.path.insert(0, "src")

from enterprise_agent_platform.contracts.commands import (
    CreateRunCommand,
    FollowupCommand,
)
from enterprise_agent_platform.contracts.enums import (
    AttemptState,
    ExecutionUnitState,
    RunState,
)
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.followup import FollowupService
from enterprise_agent_platform.control.scheduler import FairScheduler
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.execution.pipe_transport import (
    OP_COMMIT_CHECKPOINT,
    OP_COMMIT_FINAL,
    OP_RESTORE,
)
from enterprise_agent_platform.execution.subprocess_orchestrator import (
    SubprocessOrchestrator,
)
from enterprise_agent_platform.execution.runtime import _AGENT_STATE_SCHEMA_VERSION
from enterprise_agent_platform.persistence import InMemoryPlatformStore


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

TENANT = "demo-memory"

AGENT_STATE = {
    "system_prompt": "## Task Intent\n\nAnalyze demo",
    "thinking_level": "off",
    "model": {"api": "deepseek", "provider": "deepseek", "id": "deepseek-chat"},
    "tools": [
        {
            "name": "file_read",
            "description": "read a workspace file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Analyze demo"}]},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "I will inspect the resource."}],
            "stop_reason": "end_turn",  # proxy terminology, must survive roundtrip
        },
    ],
}


def _ctx() -> RequestContext:
    return RequestContext(
        tenant_id=TENANT,
        actor_id="tester",
        scopes=("runs:create", "runs:read", "runs:execute"),
        request_id="verify-checkpoint",
    )


async def _bootstrap(store, control, ctx, run_id, unit, key):
    """reserve + activate (mirrors SubprocessOrchestrator._op_bootstrap)."""
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


def _runtime_context(reservation_owner_context: dict) -> dict:
    return reservation_owner_context


async def main(store_name: str) -> None:
    global TENANT
    run_key = uuid4().hex[:8]  # fresh run per invocation (idempotency digests embed run_id)
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
            intent="Analyze demo",
            resource_refs=("synthetic:case-1",),
            parameters={},
            host_context_ref=None,
        ),
        idempotency_key=f"verify:create-run:{store_name}:{run_key}",
    )
    unit = await store.get_primary_unit(ctx.tenant_id, run.run_id)
    initial = await store.get_checkpoint(ctx.tenant_id, unit.current_checkpoint_id)
    assert initial.agent_state == {}
    assert initial.agent_state_schema_version == "pi-agent-core/v1"
    print(f"[1] run created; initial checkpoint agent_state={initial.agent_state}")

    # ── 2. Bootstrap (attempt CLAIMED, unit EXECUTING, run RUNNING) ──
    transition_key = f"verify:t-run:{store_name}:{run_key}"
    reservation, lease = await _bootstrap(store, control, ctx, run.run_id, unit, transition_key)
    context_kwargs = {
        "attempt_id": reservation.attempt.attempt_id,
        "generation": reservation.attempt.generation,
        "lease_owner": "verify:owner",
        "lease_version": lease.version,
    }
    ticket = await _make_ticket(store, reservation, unit, transition_key)
    print(
        f"[2] bootstrapped attempt={reservation.attempt.attempt_id} "
        f"lease_version={lease.version} status={reservation.attempt.status.value}"
    )

    # ── 3. Mid-run turn commit (OP_COMMIT_CHECKPOINT) ──
    result = await orchestrator._handle(
        ticket,
        ctx,
        OP_COMMIT_CHECKPOINT,
        {
            "context": context_kwargs,
            "agent_state": AGENT_STATE,
            "agent_state_schema_version": _AGENT_STATE_SCHEMA_VERSION,
        },
    )
    checkpoint_1 = await store.get_checkpoint(ctx.tenant_id, result["checkpoint_id"])
    assert checkpoint_1.checkpoint_seq == 1
    assert checkpoint_1.agent_state == AGENT_STATE
    attempt = await store.get_attempt(ctx.tenant_id, reservation.attempt.attempt_id)
    assert attempt.status is AttemptState.RUNNING, "attempt resumes RUNNING after commit"
    print(
        f"[3] turn checkpoint committed: seq={checkpoint_1.checkpoint_seq} "
        f"agent_state_messages={len(checkpoint_1.agent_state['messages'])} "
        f"attempt={attempt.status.value}"
    )

    # ── 4. Final commit (OP_COMMIT_FINAL): snapshot + SUCCEEDED ──
    result = await orchestrator._handle(
        ticket,
        ctx,
        OP_COMMIT_FINAL,
        {
            "context": context_kwargs,
            "summary": "Analysis complete: the resource is canonical.",
            "agent_state": AGENT_STATE,
            "agent_state_schema_version": _AGENT_STATE_SCHEMA_VERSION,
        },
    )
    checkpoint_2 = await store.get_checkpoint(ctx.tenant_id, result["checkpoint_id"])
    assert checkpoint_2.checkpoint_seq == 2
    assert checkpoint_2.agent_state == AGENT_STATE
    assert checkpoint_2.workflow_cursor.get("summary")
    run_after = await store.get_run(ctx.tenant_id, run.run_id)
    unit_after = await store.get_primary_unit(ctx.tenant_id, run.run_id)
    assert run_after.status is RunState.SUCCEEDED
    assert unit_after.status is ExecutionUnitState.SUCCEEDED
    print(
        f"[4] final checkpoint: seq={checkpoint_2.checkpoint_seq} "
        f"run={run_after.status.value} cursor.summary present"
    )

    # ── 5. Follow-up on terminal Run → RECOVERING + PENDING ──
    followups = FollowupService(
        store, control=control, sessions=None, answer_timeout_seconds=30.0
    )
    followup_task = asyncio.create_task(
        followups.followup(
            ctx,
            run.run_id,
            FollowupCommand(
                run_id=run.run_id,
                question="Why did the summary omit case 3?",
                client_followup_id="followup-cp-1",
            ),
            idempotency_key=f"followup-cp-1:{store_name}:{run_key}",
        )
    )
    # Wait until the Run has been reactivated by queue_followup.
    for _ in range(100):
        current = await store.get_run(ctx.tenant_id, run.run_id)
        if current.status is RunState.RECOVERING:
            break
        await asyncio.sleep(0.05)
    assert current.status is RunState.RECOVERING
    pending = await store.list_followup_requests(ctx.tenant_id, run.run_id)
    assert len(pending) == 1 and pending[0].status == "PENDING"
    print(f"[5] followup queued: run={current.status.value} pending={pending[0].followup_id}")

    # ── 6. Scheduler claims the new Attempt ──
    #    FairScheduler is tenant-round-robin (production semantics); on shared
    #    persistent stores it may first claim leftovers from previous runs, so we
    #    loop until we claim *our* RECOVERING run (isolated backends match on the
    #    first claim; assertions below apply to the claimed ticket regardless).
    scheduler = FairScheduler(store, control)
    ticket_fu = None
    for _ in range(25):
        candidate = await scheduler.claim_ready_work("verify-worker-fu")
        if candidate is None:
            break
        if candidate.run_id == run.run_id:
            ticket_fu = candidate
            break
    assert ticket_fu is not None, "scheduler never claimed the follow-up run"
    assert ticket_fu.generation == reservation.attempt.generation + 1
    assert ticket_fu.source_checkpoint_id == checkpoint_2.checkpoint_id
    print(
        f"[6] follow-up attempt claimed: gen={ticket_fu.generation} "
        f"source_checkpoint={ticket_fu.source_checkpoint_id}"
    )

    # ── 7. OP_RESTORE: fresh child rehydrates history from agent_state ──
    restore = await orchestrator._op_restore(ticket_fu, {})
    assert restore["checkpoint_id"] == checkpoint_2.checkpoint_id
    assert restore["agent_state"] == AGENT_STATE, "restore must carry the Agent snapshot"
    assert restore["agent_state_schema_version"] == _AGENT_STATE_SCHEMA_VERSION
    cursor = restore["workflow_cursor"]
    assert cursor["followup_question"] == "Why did the summary omit case 3?"
    print(
        f"[7] restore: agent_state_messages={len(restore['agent_state']['messages'])} "
        f"followup_question={cursor['followup_question']!r}"
    )

    # ── 8. New Attempt answers via OP_COMMIT_FINAL; answer written back ──
    # (the fresh child always bootstraps first: activate_lease -> CLAIMED)
    fu_attempt = await store.get_attempt(ctx.tenant_id, ticket_fu.attempt_id)
    fu_lease = await store.get_lease_for_attempt(ctx.tenant_id, ticket_fu.attempt_id)
    fu_active = await control.activate_lease(
        ctx,
        ticket_fu.attempt_id,
        ticket_fu.generation,
        owner="verify:owner",
        expected_lease_version=fu_lease.version,
    )
    fu_context = {
        "attempt_id": ticket_fu.attempt_id,
        "generation": ticket_fu.generation,
        "lease_owner": "verify:owner",
        "lease_version": fu_active.version,
    }
    result = await orchestrator._handle(
        ticket_fu,
        ctx,
        OP_COMMIT_FINAL,
        {
            "context": fu_context,
            "summary": "Because case 3 is out of scope for this analysis.",
            "agent_state": restore["agent_state"],  # rehydrated + question appended
            "agent_state_schema_version": _AGENT_STATE_SCHEMA_VERSION,
        },
    )
    checkpoint_3 = await store.get_checkpoint(ctx.tenant_id, result["checkpoint_id"])
    assert checkpoint_3.checkpoint_seq == 3
    assert checkpoint_3.agent_state == AGENT_STATE
    answered_rec = await store.get_followup_request(ctx.tenant_id, pending[0].followup_id)
    assert answered_rec.status == "ANSWERED" and answered_rec.answer
    final_run = await store.get_run(ctx.tenant_id, run.run_id)
    assert final_run.status is RunState.SUCCEEDED
    print(
        f"[8] answer committed: seq={checkpoint_3.checkpoint_seq} "
        f"answer={answered_rec.answer!r} run={final_run.status.value}"
    )

    answer = await followup_task
    assert answer.answer == answered_rec.answer
    print(f"[9] FollowupService returned persisted answer: {answer.answer!r}")

    print("\nPASS: checkpoint chain (mid-run + final + restore + follow-up) verified")


async def _make_ticket(store, reservation, unit, key):
    from enterprise_agent_platform.domain.records import DispatchTicket

    del store, unit
    return DispatchTicket(
        worker_id=key,
        tenant_id=TENANT,
        run_id=reservation.attempt.run_id,
        execution_unit_id=reservation.attempt.execution_unit_id,
        attempt_id=reservation.attempt.attempt_id,
        lease_id=reservation.lease.lease_id,
        generation=reservation.attempt.generation,
        source_checkpoint_id=reservation.attempt.source_checkpoint_id,
    )


PASS_LINE = "PASS"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="verify checkpoint+followup chain")
    parser.add_argument("--store", choices=("memory", "sqlite", "pg"), default="memory",
                        help="store backend: memory | sqlite L1 | pg (AGENT_PLATFORM_DATABASE_URL)")
    args = parser.parse_args()
    asyncio.run(main(args.store))