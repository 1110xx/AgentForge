"""End-to-end verification of the follow-up-as-new-Attempt chain.

Simulates the complete lifecycle without a live scheduler:
  1. create_run → QUEUED + COMMITTED checkpoint
  2. complete the run (Attempt SUCCEEDED, Unit SUCCEEDED, Run SUCCEEDED)
  3. FollowupService.followup() → queue_followup (Run/Unit → RECOVERING,
     FollowupRequestRecord PENDING)
  4. FairScheduler can now claim the unit again (reserve_attempt succeeds):
     the new follow-up Attempt is schedulable — proving the fresh Runner path.
  5. A manually completed follow-up Attempt writes the answer back.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

sys.path.insert(0, "src")

from enterprise_agent_platform.contracts.commands import CreateRunCommand, FollowupCommand
from enterprise_agent_platform.contracts.enums import (
    AttemptState,
    CheckpointState,
    EntityType,
    ExecutionLeaseState,
    ExecutionUnitState,
    RunState,
)
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.followup import FollowupService
from enterprise_agent_platform.control.scheduler import FairScheduler
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.domain.fsm import transition
from enterprise_agent_platform.persistence import InMemoryPlatformStore


def _ctx() -> RequestContext:
    return RequestContext(
        tenant_id="demo",
        actor_id="tester",
        scopes=("runs:create", "runs:read", "runs:execute"),
        request_id="verify-followup",
    )


async def _complete_run(store, control, ctx, run_id) -> None:
    """Drive Run/Unit/Attempt/Lease through the terminal SUCCEEDED state."""
    run = await store.get_run(ctx.tenant_id, run_id)
    unit = await store.get_primary_unit(ctx.tenant_id, run_id)
    checkpoint = await store.get_checkpoint(ctx.tenant_id, unit.current_checkpoint_id)

    # Minimal bootstrap: reserve + activate (mirrors SubprocessOrchestrator).
    reservation = await control.reserve_attempt(
        ctx, unit.execution_unit_id, checkpoint.checkpoint_id, unit.version,
        transition_key="verify:t1",
    )
    attempt = reservation.attempt
    lease = reservation.lease
    await control.activate_lease(
        ctx, attempt.attempt_id, attempt.generation, owner="verify:owner",
        expected_lease_version=lease.version,
    )

    # Mark terminal states directly in a transaction.
    async with store.transaction() as tx:
        now = await tx.db_now()
        run = await tx.lock_run(ctx.tenant_id, run_id)
        unit = await tx.lock_execution_unit(ctx.tenant_id, unit.execution_unit_id)
        attempt = await tx.get_attempt(ctx.tenant_id, attempt.attempt_id)
        lease = await tx.get_lease_for_attempt(ctx.tenant_id, attempt.attempt_id)

        # CLAIMED -> RUNNING -> SUCCEEDED (two legal hops, versioned CAS)
        transition(EntityType.ATTEMPT, attempt.status, AttemptState.RUNNING, None)
        running = replace(attempt, status=AttemptState.RUNNING,
                          version=attempt.version + 1)
        await tx.replace_attempt_cas(running, attempt.version)
        transition(EntityType.ATTEMPT, running.status, AttemptState.SUCCEEDED, None)
        done_attempt = replace(running, status=AttemptState.SUCCEEDED,
                               version=running.version + 1, ended_at=now)
        await tx.replace_attempt_cas(done_attempt, running.version)

        transition(EntityType.EXECUTION_LEASE, lease.state, ExecutionLeaseState.RELEASED, None)
        done_lease = replace(lease, state=ExecutionLeaseState.RELEASED,
                             version=lease.version + 1, released_at=now)
        await tx.replace_lease_cas(done_lease, lease.version)

        transition(EntityType.EXECUTION_UNIT, unit.status, ExecutionUnitState.SUCCEEDED, None)
        done_unit = replace(unit, status=ExecutionUnitState.SUCCEEDED,
                            version=unit.version + 1)
        await tx.replace_execution_unit_cas(done_unit, unit.version)

        transition(EntityType.RUN, run.status, RunState.SUCCEEDED, None)
        done_run = replace(run, status=RunState.SUCCEEDED, version=run.version + 1, ended_at=now)
        await tx.replace_run_cas(done_run, run.version)


async def main() -> None:
    store = InMemoryPlatformStore()
    control = ControlPlaneService(store)
    # Short timeout so the no-scheduler harness exercises the timeout path fast.
    followups = FollowupService(store, control=control, sessions=None,
                                answer_timeout_seconds=3.0)

    # 1. Create the run
    run = await control.create_run(
        _ctx(),
        CreateRunCommand(
            workflow_type="verify",
            intent="Summarize the demo dataset",
            resource_refs=("synthetic:case-1",),
            parameters={},
            host_context_ref=None,
        ),
        idempotency_key="verify:create-run",
    )
    print(f"[1] run created: {run.run_id} status={run.status.value}")

    # 2. Complete the run
    await _complete_run(store, control, _ctx(), run.run_id)
    print("[2] run completed -> SUCCEEDED")

    # 3. Follow-up on a terminal run → must schedule (not inline)
    from enterprise_agent_platform.control.followup import FollowupError
    try:
        await followups.followup(
            _ctx(),
            run.run_id,
            FollowupCommand(
                run_id=run.run_id,
                question="Why did the summary omit case 3?",
                client_followup_id="followup-verify-1",
            ),
            idempotency_key="followup-verify-1",
        )
        raise AssertionError("expected FOLLOWUP_TIMEOUT without a scheduler")
    except FollowupError as error:
        assert error.code == "FOLLOWUP_TIMEOUT", error.code
        print("[3] followup queued; no scheduler in harness -> FOLLOWUP_TIMEOUT (expected)")

    # 4. Verify FSM reactivation + schedulability
    run_after = await store.get_run(ctx_tenant := "demo", run.run_id)
    unit_after = await store.get_primary_unit(ctx_tenant, run.run_id)
    print(f"[4] after queue_followup: run={run_after.status.value} unit={unit_after.status.value}")
    assert run_after.status is RunState.RECOVERING, run_after.status
    assert unit_after.status is ExecutionUnitState.RECOVERING, unit_after.status

    pending = await store.list_followup_requests(ctx_tenant, run.run_id)
    assert len(pending) == 1 and pending[0].status == "PENDING"
    print(f"[5] durable FollowupRequestRecord PENDING: {pending[0].followup_id}")

    # 5. Scheduler can now claim the follow-up work (new Attempt + generation+1)
    scheduler = FairScheduler(store, control)
    ticket = await scheduler.claim_ready_work("verify-worker")
    assert ticket is not None, "scheduler must claim the reactivated unit"
    print(
        f"[6] scheduler claimed follow-up: attempt={ticket.attempt_id} "
        f"gen={ticket.generation} source_checkpoint={ticket.source_checkpoint_id}"
    )

    # 6. Simulate the fresh Runner answering: restore cursor carries the question,
    #    then commit writes the answer back (as SubprocessOrchestrator does).
    orchestrator = await _import_orchestrator(store, control)
    restore = await orchestrator._op_restore(ticket, {})
    cursor = restore["workflow_cursor"]
    assert cursor.get("followup_question") == "Why did the summary omit case 3?"
    print(f"[7] restore cursor carries followup_question: {cursor['followup_question']!r}")

    # 7. Answer written back (the orchestrator's commit path)
    answered = await orchestrator._answer_pending_followup(ticket, "Because case 3 is out of scope.")
    assert answered is True
    answered_rec = await store.get_followup_request(ctx_tenant, pending[0].followup_id)
    assert answered_rec.status == "ANSWERED" and answered_rec.answer
    print(f"[8] answer persisted: {answered_rec.answer!r}")

    print("\nPASS: follow-up-as-new-Attempt chain verified end-to-end")


async def _import_orchestrator(store, control):
    from enterprise_agent_platform.execution.subprocess_orchestrator import SubprocessOrchestrator

    return SubprocessOrchestrator(
        store=store,
        control=control,
        run_sessions=None,
        resource_resolver=None,
    )


if __name__ == "__main__":
    asyncio.run(main())