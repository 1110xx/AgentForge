"""Phase 4.5 (4.4 leftover #2): stale-PROVISIONING recovery.

A scheduler restart between ``reserve_attempt`` and the Job/Pod dispatch
leaves the Attempt stuck in PROVISIONING with a RESERVED Lease that no
Runtime will ever expire (the runtime-side ``recover_expired_lease`` only
handles ACTIVE leases). The Run/Unit stays blocked by the
one-active-Attempt/Lease guard indefinitely — the exact stall observed in
the 4.4 soak when a sandbox quota reclaimed an Orchestrator mid-flight.

This pins the sweep contract:

* ``list_stale_provisioning(now)`` returns exactly the pairs whose RESERVED
  Lease lapsed its ``provision_deadline`` (pairs with a future deadline are
  excluded — the scheduler is still within its window);
* ``recover_stale_provisioning`` terminalizes the orphan (Attempt->FAILED,
  Lease->RELEASED), re-opens the Unit to RECOVERING (the Run flips to
  RECOVERING only when the fsm allows it — a QUEUED first-try Run stays
  QUEUED and no bogus RUN_STATUS_CHANGED event is emitted), reserves a
  same-generation successor Attempt+Lease with a fresh 10-minute
  ``provision_deadline``, emits attempt-lifecycle failure+provisioning
  events, and queues ``attempt.provisioning.requested`` in the outbox so the
  scheduler re-claims the successor on its next poll;
* recovery is idempotent: a second pass on the same Attempt is a no-op, and
  the sweep goes quiet once the pair is reclaimed.

Both the in-memory dev store and the SQLAlchemy (sqlite L1) implementation
must behave identically.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from enterprise_agent_platform.contracts.enums import (
    AttemptState,
    EventType,
    ExecutionLeaseState,
    ExecutionUnitState,
    RunState,
)
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.reconciler import recover_stale_provisioning
from enterprise_agent_platform.domain.records import (
    AttemptRecord,
    ExecutionLeaseRecord,
    ExecutionUnitRecord,
    RunRecord,
)
from enterprise_agent_platform.persistence.database import (
    create_schema,
    create_sqlite_l1_engine,
)
from enterprise_agent_platform.persistence.memory import InMemoryPlatformStore
from enterprise_agent_platform.persistence.sqlalchemy_store import SqlAlchemyPlatformStore

TENANT = "stale-provisioning-test"
ACTOR = "scheduler:test"

RUN_ID = "run-stale-001"
UNIT_ID = "unit-stale-001"
ATTEMPT_ID = "attempt-stale-001"
LEASE_ID = "lease-stale-001"

FUT_RUN_ID = "run-future-001"
FUT_UNIT_ID = "unit-future-001"
FUT_ATTEMPT_ID = "attempt-future-001"
FUT_LEASE_ID = "lease-future-001"

Q_RUN_ID = "run-queued-001"
Q_UNIT_ID = "unit-queued-001"
Q_ATTEMPT_ID = "attempt-queued-001"
Q_LEASE_ID = "lease-queued-001"


def _run(now: datetime, run_id: str, status: RunState) -> RunRecord:
    return RunRecord(
        tenant_id=TENANT,
        run_id=run_id,
        owner_id=ACTOR,
        parent_run_id=None,
        workflow_type="synthetic-analysis",
        intent="stale provisioning recovery scenario",
        resource_refs=("synthetic-case:case-42",),
        parameters={"analysis_mode": "summary", "max_items": 10},
        host_context_ref="reference-context:demo",
        status=status,
        status_reason=None,
        version=1,
        last_event_seq=0,
        fsm_version="enterprise-agent/fsm/v1",
        cancel_requested_by=None,
        cancel_requested_at=None,
        cancel_reason=None,
        created_at=now,
        updated_at=now,
        ended_at=None,
    )


def _unit(now: datetime, run_id: str, unit_id: str) -> ExecutionUnitRecord:
    return ExecutionUnitRecord(
        tenant_id=TENANT,
        execution_unit_id=unit_id,
        run_id=run_id,
        role="orchestrator",
        status=ExecutionUnitState.DISPATCHABLE,
        version=1,
        current_checkpoint_id=None,
        next_generation=1,
        runtime_profile="sandbox",
        created_at=now,
        updated_at=now,
    )


def _attempt(now: datetime, run_id: str, unit_id: str, attempt_id: str) -> AttemptRecord:
    return AttemptRecord(
        tenant_id=TENANT,
        attempt_id=attempt_id,
        run_id=run_id,
        execution_unit_id=unit_id,
        step_id=None,
        generation=1,
        status=AttemptState.PROVISIONING,
        version=1,
        runtime_profile="sandbox",
        source_checkpoint_id=None,
        reservation_key=f"reserve:{attempt_id}",
        created_at=now,
        updated_at=now,
        started_at=None,
        ended_at=None,
        failure_id=None,
    )


def _lease(
    now: datetime,
    run_id: str,
    unit_id: str,
    attempt_id: str,
    lease_id: str,
    *,
    stale: bool,
) -> ExecutionLeaseRecord:
    deadline = now - timedelta(minutes=5) if stale else now + timedelta(minutes=9)
    return ExecutionLeaseRecord(
        tenant_id=TENANT,
        lease_id=lease_id,
        run_id=run_id,
        execution_unit_id=unit_id,
        attempt_id=attempt_id,
        generation=1,
        state=ExecutionLeaseState.RESERVED,
        owner=None,
        version=1,
        activated_from_version=None,
        provision_deadline=deadline,
        heartbeat_at=None,
        expires_at=None,
        released_at=None,
        created_at=now,
        updated_at=now,
    )


async def _seed(
    store,
    now: datetime,
    *,
    run_id: str,
    unit_id: str,
    attempt_id: str,
    lease_id: str,
    run_status: RunState,
    stale: bool,
) -> None:
    async with store.transaction() as tx:
        await tx.insert_run(_run(now, run_id, run_status))
        await tx.insert_execution_unit(_unit(now, run_id, unit_id))
        await tx.insert_attempt(_attempt(now, run_id, unit_id, attempt_id))
        await tx.insert_lease(_lease(now, run_id, unit_id, attempt_id, lease_id, stale=stale))


async def _scenario(store, now_holder: dict[str, datetime]) -> None:
    t0 = now_holder["now"]

    # ── 1. stale pair (Run RUNNING: mid-execution scheduler crash) ──
    await _seed(
        store,
        t0,
        run_id=RUN_ID,
        unit_id=UNIT_ID,
        attempt_id=ATTEMPT_ID,
        lease_id=LEASE_ID,
        run_status=RunState.RUNNING,
        stale=True,
    )

    # ── 2. scan returns exactly the lapsed pair ──
    scan = await store.list_stale_provisioning(t0)
    assert len(scan) == 1
    pair_attempt, pair_lease = scan[0]
    assert pair_attempt.attempt_id == ATTEMPT_ID
    assert pair_attempt.status is AttemptState.PROVISIONING
    assert pair_lease.lease_id == LEASE_ID
    assert pair_lease.state is ExecutionLeaseState.RESERVED

    # ── 3. a pair whose deadline is still ahead is excluded ──
    await _seed(
        store,
        t0,
        run_id=FUT_RUN_ID,
        unit_id=FUT_UNIT_ID,
        attempt_id=FUT_ATTEMPT_ID,
        lease_id=FUT_LEASE_ID,
        run_status=RunState.QUEUED,
        stale=False,
    )
    scan = await store.list_stale_provisioning(t0)
    assert len(scan) == 1 and scan[0][0].attempt_id == ATTEMPT_ID

    # ── 4. recover ──
    ctx = RequestContext(
        tenant_id=TENANT,
        actor_id=ACTOR,
        scopes=("runs:execute",),
        request_id="stale-sweep:unit-001",
    )
    result = await recover_stale_provisioning(store, ctx, attempt_id=ATTEMPT_ID)
    assert result is not None
    successor = result.successor_attempt
    successor_lease = result.successor_lease
    assert successor.status is AttemptState.PROVISIONING
    assert successor.generation == 2  # reserve chain: next(1) + 1
    assert successor.attempt_id != ATTEMPT_ID
    assert successor_lease.state is ExecutionLeaseState.RESERVED
    assert successor_lease.attempt_id == successor.attempt_id
    assert successor_lease.provision_deadline == t0 + timedelta(minutes=10)

    # ── 5. orphan terminalized + Unit/Run re-opened ──
    async with store.transaction() as tx:
        failed = await tx.get_attempt(TENANT, ATTEMPT_ID)
        assert failed.status is AttemptState.FAILED and failed.version == 2
        assert failed.ended_at == t0
        lease = await tx.get_lease_for_attempt(TENANT, ATTEMPT_ID)
        assert lease.state is ExecutionLeaseState.RELEASED and lease.released_at == t0
        unit = await tx.get_execution_unit(TENANT, UNIT_ID)
        assert unit.status is ExecutionUnitState.RECOVERING and unit.version == 2
        assert unit.next_generation == 2  # tracks the successor generation
        run = await tx.get_run(TENANT, RUN_ID)
        assert run.status is RunState.RECOVERING and run.version == 2
        assert run.last_event_seq == 3

    # ── 6. event log: attempt FAILED, attempt PROVISIONING, run RECOVERING ──
    async with store.transaction() as tx:
        events = await tx.list_events_for_run(TENANT, RUN_ID)
    assert [e.event_seq for e in events] == [1, 2, 3]
    assert [e.event_type for e in events] == [
        EventType.ATTEMPT_LIFECYCLE,
        EventType.ATTEMPT_LIFECYCLE,
        EventType.RUN_STATUS_CHANGED,
    ]
    assert events[0].payload.status is AttemptState.FAILED
    assert events[0].payload.attempt_id == ATTEMPT_ID
    assert events[1].payload.status is AttemptState.PROVISIONING
    assert events[1].payload.attempt_id == successor.attempt_id
    assert events[2].payload.previous is RunState.RUNNING
    assert events[2].payload.current is RunState.RECOVERING

    # ── 7. outbox carries the successor reservation for the next poll ──
    outbox = await store.list_outbox(TENANT, RUN_ID)
    assert any(
        m.topic == "attempt.provisioning.requested"
        and m.payload["attempt_id"] == successor.attempt_id
        for m in outbox
    )

    # ── 8. audit trail ──
    audits = await store.list_audit_events(TENANT, RUN_ID)
    assert any(a.action == "execution.provisioning.recovered" for a in audits)

    # ── 9. idempotent: second pass is a no-op; sweep goes quiet ──
    again = await recover_stale_provisioning(store, ctx, attempt_id=ATTEMPT_ID)
    assert again is None
    scan = await store.list_stale_provisioning(t0)
    assert scan == ()

    # ── 10. QUEUED-Run edge (first-try orchestration crash): Run stays QUEUED ──
    await _seed(
        store,
        t0,
        run_id=Q_RUN_ID,
        unit_id=Q_UNIT_ID,
        attempt_id=Q_ATTEMPT_ID,
        lease_id=Q_LEASE_ID,
        run_status=RunState.QUEUED,
        stale=True,
    )
    qctx = RequestContext(
        tenant_id=TENANT,
        actor_id=ACTOR,
        scopes=("runs:execute",),
        request_id="stale-sweep:unit-queued",
    )
    qresult = await recover_stale_provisioning(store, qctx, attempt_id=Q_ATTEMPT_ID)
    assert qresult is not None
    async with store.transaction() as tx:
        qrun = await tx.get_run(TENANT, Q_RUN_ID)
        assert qrun.status is RunState.QUEUED  # fsm forbids QUEUED->RECOVERING
        assert qrun.last_event_seq == 2  # no bogus run-status event
        qunit = await tx.get_execution_unit(TENANT, Q_UNIT_ID)
        assert qunit.status is ExecutionUnitState.RECOVERING
        qevents = await tx.list_events_for_run(TENANT, Q_RUN_ID)
    assert [e.event_type for e in qevents] == [
        EventType.ATTEMPT_LIFECYCLE,
        EventType.ATTEMPT_LIFECYCLE,
    ]


def test_stale_provisioning_recovery_memory() -> None:
    memory_now = {"now": datetime(2026, 8, 22, 9, 30, 0, tzinfo=UTC)}
    memory = InMemoryPlatformStore(clock=lambda: memory_now["now"])
    asyncio.run(_scenario(memory, memory_now))


def test_stale_provisioning_recovery_sqlite() -> None:
    sqlite_now = {"now": datetime(2026, 8, 22, 9, 30, 0, tzinfo=UTC)}
    engine = create_sqlite_l1_engine()

    async def scenario() -> None:
        await create_schema(engine)
        store = SqlAlchemyPlatformStore(
            async_sessionmaker(engine, expire_on_commit=False),
            sqlite_l1_clock=lambda: sqlite_now["now"],
        )
        await _scenario(store, sqlite_now)
        await engine.dispose()

    asyncio.run(scenario())