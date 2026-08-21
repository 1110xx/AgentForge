"""Shared terminal-state completer for Run / Attempt / Lease / Unit lifecycle.

Extracted from ``LocalRuntime._complete_run`` / ``_fail_run`` (SDD §13.2 risk 1):
``SubprocessOrchestrator`` reused LocalRuntime's *private* methods as its
``_completer``, which was an unclean coupling. This module centralises the
terminal transitions into one public completer that both the inline
``LocalRuntime`` (fallback) and the child-process ``SubprocessOrchestrator``
share — the completer only talks to the store transaction + CAS optimistic
locks, never to control-plane services or model sessions.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from enterprise_agent_platform.contracts.enums import (
    AttemptState,
    EntityType,
    EventType,
    ExecutionLeaseState,
    ExecutionUnitState,
    RunState,
)
from enterprise_agent_platform.contracts.events import (
    AttemptLifecyclePayload,
    EnterpriseEventEnvelope,
    RunStatusChangedPayload,
)
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.domain.fsm import transition
from enterprise_agent_platform.domain.records import (
    AuditEventRecord,
    DispatchTicket,
    OutboxMessageRecord,
    RunRecord,
)
from enterprise_agent_platform.persistence.protocol import PlatformStore

logger = logging.getLogger(__name__)


class RunCompleter:
    """Terminal state machine: drives Run to SUCCEEDED / FAILED with events.

    Responsibilities (one transaction per call):
    - Lock Run / ExecutionUnit / Attempt / Lease aggregates
    - Apply valid FSM transitions (Attempt → SUCCEEDED/FAILED, Lease → RELEASED,
      Unit → SUCCEEDED/FAILED, Run → SUCCEEDED/FAILED)
    - Emit ``ATTEMPT_LIFECYCLE`` + ``RUN_STATUS_CHANGED`` events
    - Write audit + outbox ``run.terminal`` records
    """

    def __init__(self, store: PlatformStore) -> None:
        self._store = store

    async def complete_run(
        self,
        ctx: RequestContext,
        ticket: DispatchTicket,
        run: RunRecord,
    ) -> None:
        """Transition the Run to SUCCEEDED with proper events."""
        async with self._store.transaction() as tx:
            now = await tx.db_now()

            # Lock all aggregates
            run = await tx.lock_run(ticket.tenant_id, run.run_id)
            unit = await tx.lock_execution_unit(ticket.tenant_id, ticket.execution_unit_id)
            attempt = await tx.get_attempt(ticket.tenant_id, ticket.attempt_id)
            lease = await tx.get_lease_for_attempt(ticket.tenant_id, ticket.attempt_id)

            # Validate state
            if run.status not in {RunState.QUEUED, RunState.RUNNING}:
                logger.warning("Run %s not in RUNNING state, skipping terminal transition", run.run_id)
                return

            # Transition: Attempt → RUNNING then SUCCEEDED. A mid-run checkpoint
            # commit may already have moved the Attempt to RUNNING (CHECKPOINTING
            # is resumed back to RUNNING by commit_checkpoint), so only bump the
            # status when it is not already RUNNING.
            if attempt.status is AttemptState.RUNNING:
                running_attempt = attempt
                running_version = attempt.version
            else:
                transition(EntityType.ATTEMPT, attempt.status, AttemptState.RUNNING, None)
                running_attempt = replace(
                    attempt,
                    status=AttemptState.RUNNING,
                    version=attempt.version + 1,
                    updated_at=now,
                )
                await tx.replace_attempt_cas(running_attempt, attempt.version)
                running_version = running_attempt.version

            # Attempt SUCCEEDED
            transition(EntityType.ATTEMPT, running_attempt.status, AttemptState.SUCCEEDED, None)
            succeeded_attempt = replace(
                running_attempt,
                status=AttemptState.SUCCEEDED,
                version=running_version + 1,
                updated_at=now,
                ended_at=now,
            )

            # Lease RELEASED
            transition(EntityType.EXECUTION_LEASE, lease.state, ExecutionLeaseState.RELEASED, None)
            released_lease = replace(
                lease,
                state=ExecutionLeaseState.RELEASED,
                version=lease.version + 1,
                released_at=now,
                updated_at=now,
            )

            # Unit SUCCEEDED
            transition(EntityType.EXECUTION_UNIT, unit.status, ExecutionUnitState.SUCCEEDED, None)
            succeeded_unit = replace(
                unit,
                status=ExecutionUnitState.SUCCEEDED,
                version=unit.version + 1,
                updated_at=now,
            )

            # Run SUCCEEDED
            transition(EntityType.RUN, run.status, RunState.SUCCEEDED, None)
            succeeded_run = replace(
                run,
                status=RunState.SUCCEEDED,
                status_reason=None,
                version=run.version + 1,
                last_event_seq=run.last_event_seq + 2,
                updated_at=now,
                ended_at=now,
            )

            # Events
            attempt_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self._store.new_id("event"),
                tenant_id=ticket.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 1,
                event_type=EventType.ATTEMPT_LIFECYCLE,
                occurred_at=now,
                producer_service="platform-completer",
                payload_schema="attempt-lifecycle/v1",
                payload=AttemptLifecyclePayload(
                    kind="attempt.lifecycle",
                    attempt_id=ticket.attempt_id,
                    status=AttemptState.SUCCEEDED,
                ),
                attempt_id=ticket.attempt_id,
                trace_id=ctx.trace_id,
            )
            run_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self._store.new_id("event"),
                tenant_id=ticket.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 2,
                event_type=EventType.RUN_STATUS_CHANGED,
                occurred_at=now,
                producer_service="platform-completer",
                payload_schema="run-status/v1",
                payload=RunStatusChangedPayload(
                    kind="run.status.changed",
                    previous=run.status,
                    current=RunState.SUCCEEDED,
                ),
                attempt_id=ticket.attempt_id,
                causation_event_id=attempt_event.event_id,
                trace_id=ctx.trace_id,
            )

            # Persist
            await tx.replace_attempt_cas(succeeded_attempt, running_attempt.version)
            await tx.replace_lease_cas(released_lease, lease.version)
            await tx.replace_execution_unit_cas(succeeded_unit, unit.version)
            await tx.replace_run_cas(succeeded_run, run.version)
            await tx.append_event(attempt_event, run.last_event_seq)
            await tx.append_event(run_event, attempt_event.event_seq)

            # Audit + outbox
            await tx.insert_audit(
                AuditEventRecord(
                    tenant_id=ticket.tenant_id,
                    audit_event_id=self._store.new_id("audit"),
                    run_id=run.run_id,
                    actor_id=ctx.actor_id,
                    action="run.succeeded",
                    entity_type="run",
                    entity_id=run.run_id,
                    entity_version=succeeded_run.version,
                    outcome="SUCCEEDED",
                    trace_id=ctx.trace_id,
                    details={"attempt_id": ticket.attempt_id},
                    created_at=now,
                )
            )
            await tx.insert_outbox(
                OutboxMessageRecord(
                    tenant_id=ticket.tenant_id,
                    message_id=self._store.new_id("outbox"),
                    run_id=run.run_id,
                    topic="run.terminal",
                    payload={"run_id": run.run_id},
                    event_id=run_event.event_id,
                    aggregate_version=succeeded_run.version,
                    created_at=now,
                    published_at=None,
                )
            )

    async def fail_run(
        self,
        ctx: RequestContext,
        ticket: DispatchTicket,
        run: RunRecord,
        error: Exception | None,
    ) -> None:
        """Transition the Run to FAILED with proper events."""
        async with self._store.transaction() as tx:
            now = await tx.db_now()

            run = await tx.lock_run(ticket.tenant_id, run.run_id)
            unit = await tx.lock_execution_unit(ticket.tenant_id, ticket.execution_unit_id)
            attempt = await tx.get_attempt(ticket.tenant_id, ticket.attempt_id)
            lease = await tx.get_lease_for_attempt(ticket.tenant_id, ticket.attempt_id)

            if run.status in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
                logger.warning("Run %s already terminal, skipping fail", run.run_id)
                return

            # Attempt FAILED
            transition(EntityType.ATTEMPT, attempt.status, AttemptState.FAILED, None)
            failed_attempt = replace(
                attempt,
                status=AttemptState.FAILED,
                version=attempt.version + 1,
                updated_at=now,
                ended_at=now,
            )

            # Lease RELEASED
            try:
                transition(EntityType.EXECUTION_LEASE, lease.state, ExecutionLeaseState.RELEASED, None)
            except Exception:  # noqa: BLE001 - lease may already be released/expired; force terminal anyway
                lease_state = ExecutionLeaseState.EXPIRED
            else:
                lease_state = ExecutionLeaseState.RELEASED
            released_lease = replace(
                lease,
                state=lease_state,
                version=lease.version + 1,
                released_at=now,
                updated_at=now,
            )

            # Unit FAILED
            try:
                transition(EntityType.EXECUTION_UNIT, unit.status, ExecutionUnitState.FAILED, None)
                failed_unit = replace(
                    unit,
                    status=ExecutionUnitState.FAILED,
                    version=unit.version + 1,
                    updated_at=now,
                )
            except Exception:  # noqa: BLE001 - unit may already be terminal; still persist a bumped version
                failed_unit = replace(
                    unit,
                    version=unit.version + 1,
                    updated_at=now,
                )

            # Run FAILED
            try:
                transition(EntityType.RUN, run.status, RunState.FAILED, None)
            except Exception:  # noqa: BLE001,S110 - force terminal; invalid transitions are intentionally swallowed
                pass
            failed_run = replace(
                run,
                status=RunState.FAILED,
                status_reason=str(error) if error else "Runtime execution failed",
                version=run.version + 1,
                last_event_seq=run.last_event_seq + 2,
                updated_at=now,
                ended_at=now,
            )

            # Events
            attempt_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self._store.new_id("event"),
                tenant_id=ticket.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 1,
                event_type=EventType.ATTEMPT_LIFECYCLE,
                occurred_at=now,
                producer_service="platform-completer",
                payload_schema="attempt-lifecycle/v1",
                payload=AttemptLifecyclePayload(
                    kind="attempt.lifecycle",
                    attempt_id=ticket.attempt_id,
                    status=AttemptState.FAILED,
                ),
                attempt_id=ticket.attempt_id,
                trace_id=ctx.trace_id,
            )
            run_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self._store.new_id("event"),
                tenant_id=ticket.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 2,
                event_type=EventType.RUN_STATUS_CHANGED,
                occurred_at=now,
                producer_service="platform-completer",
                payload_schema="run-status/v1",
                payload=RunStatusChangedPayload(
                    kind="run.status.changed",
                    previous=run.status,
                    current=RunState.FAILED,
                ),
                attempt_id=ticket.attempt_id,
                causation_event_id=attempt_event.event_id,
                trace_id=ctx.trace_id,
            )

            await tx.replace_attempt_cas(failed_attempt, attempt.version)
            await tx.replace_lease_cas(released_lease, lease.version)
            await tx.replace_execution_unit_cas(failed_unit, unit.version)
            await tx.replace_run_cas(failed_run, run.version)
            await tx.append_event(attempt_event, run.last_event_seq)
            await tx.append_event(run_event, attempt_event.event_seq)

            await tx.insert_audit(
                AuditEventRecord(
                    tenant_id=ticket.tenant_id,
                    audit_event_id=self._store.new_id("audit"),
                    run_id=run.run_id,
                    actor_id=ctx.actor_id,
                    action="run.failed",
                    entity_type="run",
                    entity_id=run.run_id,
                    entity_version=failed_run.version,
                    outcome="FAILED",
                    trace_id=ctx.trace_id,
                    details={
                        "attempt_id": ticket.attempt_id,
                        "error": str(error) if error else None,
                    },
                    created_at=now,
                )
            )
            await tx.insert_outbox(
                OutboxMessageRecord(
                    tenant_id=ticket.tenant_id,
                    message_id=self._store.new_id("outbox"),
                    run_id=run.run_id,
                    topic="run.terminal",
                    payload={"run_id": run.run_id},
                    event_id=run_event.event_id,
                    aggregate_version=failed_run.version,
                    created_at=now,
                    published_at=None,
                )
            )


__all__ = ["RunCompleter"]