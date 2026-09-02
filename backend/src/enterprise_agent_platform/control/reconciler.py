import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta

from enterprise_agent_platform.contracts.enums import (
    AttemptState,
    CheckpointState,
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
from enterprise_agent_platform.domain.fsm import CompleteCancellation, transition
from enterprise_agent_platform.domain.records import (
    AttemptRecord,
    AuditEventRecord,
    ExecutionLeaseRecord,
    InboxMessageRecord,
    OutboxMessageRecord,
    RecoveryResult,
)
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore

from .context import RequestContext

__all__ = ["CompleteCancellation", "recover_expired_lease"]


def _payload_digest(attempt_id: str, generation: int) -> str:
    payload = json.dumps(
        {"attempt_id": attempt_id, "generation": generation},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


async def recover_expired_lease(
    store: PlatformStore,
    ctx: RequestContext,
    *,
    message_id: str,
    handler_version: str,
    attempt_id: str,
    generation: int,
    provision_window: timedelta = timedelta(minutes=10),
) -> RecoveryResult | None:
    """Fence a lost runtime and reserve exactly one successor Attempt.

    The Inbox fact and all recovery facts share one database transaction. A message
    transport may acknowledge only after this function returns successfully.
    """
    if not message_id or not handler_version:
        raise PlatformError("INTEGRITY_VIOLATION", "message_id and handler_version are required")
    if generation < 1:
        raise PlatformError("STALE_GENERATION", "generation must be positive")
    if provision_window <= timedelta(0):
        raise ValueError("provision_window must be positive")

    async with store.transaction() as tx:
        now = await tx.db_now()
        inbox = InboxMessageRecord(
            tenant_id=ctx.tenant_id,
            message_id=message_id,
            handler_version=handler_version,
            topic="execution.lease.expired",
            payload_schema="lease-expired/v1",
            payload_digest=_payload_digest(attempt_id, generation),
            processing_state="RECEIVED",
            version=1,
            received_at=now,
            processed_at=None,
            failure_code=None,
        )
        if not await tx.claim_inbox_message(inbox):
            return None

        candidate_attempt = await tx.get_attempt(ctx.tenant_id, attempt_id)
        run = await tx.lock_run(ctx.tenant_id, candidate_attempt.run_id)
        unit = await tx.lock_execution_unit(
            ctx.tenant_id, candidate_attempt.execution_unit_id
        )
        attempt = await tx.get_attempt(ctx.tenant_id, attempt_id)
        lease = await tx.get_lease_for_attempt(ctx.tenant_id, attempt_id)
        if (
            attempt.run_id != run.run_id
            or attempt.execution_unit_id != unit.execution_unit_id
            or lease.run_id != run.run_id
            or lease.execution_unit_id != unit.execution_unit_id
            or lease.attempt_id != attempt.attempt_id
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "runtime facts are inconsistent")
        if generation != attempt.generation or generation != lease.generation:
            raise PlatformError("STALE_GENERATION", "recovery generation does not match")
        if lease.state is ExecutionLeaseState.EXPIRED and attempt.status is AttemptState.LOST:
            await tx.replace_inbox_message_cas(
                replace(
                    inbox,
                    processing_state="PROCESSED",
                    version=2,
                    processed_at=now,
                ),
                inbox.version,
            )
            return None
        if lease.state is not ExecutionLeaseState.ACTIVE:
            raise PlatformError("LEASE_NOT_ACTIVE", "Lease is not active")
        if lease.expires_at is None or now < lease.expires_at:
            raise PlatformError("LEASE_NOT_EXPIRED", "Lease has not expired")
        if unit.next_generation != generation:
            raise PlatformError("STALE_GENERATION", "ExecutionUnit generation has advanced")
        if run.status is not RunState.RUNNING or unit.status is not ExecutionUnitState.EXECUTING:
            raise PlatformError("INVALID_STATE", "Run execution is not recoverable")
        if attempt.status not in {
            AttemptState.CLAIMED,
            AttemptState.RUNNING,
            AttemptState.CHECKPOINTING,
        }:
            raise PlatformError("INVALID_STATE", "Attempt is not recoverable")
        if unit.current_checkpoint_id is None:
            raise PlatformError("SOURCE_CHECKPOINT_INVALID", "Unit has no recovery Checkpoint")
        source = await tx.get_checkpoint(ctx.tenant_id, unit.current_checkpoint_id)
        if (
            source.state is not CheckpointState.COMMITTED
            or source.run_id != run.run_id
            or source.execution_unit_id != unit.execution_unit_id
        ):
            raise PlatformError(
                "SOURCE_CHECKPOINT_INVALID", "recovery source must be a committed Checkpoint"
            )

        transition(EntityType.ATTEMPT, attempt.status, AttemptState.LOST, inbox)
        transition(
            EntityType.EXECUTION_LEASE,
            lease.state,
            ExecutionLeaseState.EXPIRED,
            inbox,
        )
        transition(
            EntityType.EXECUTION_UNIT,
            unit.status,
            ExecutionUnitState.RECOVERING,
            inbox,
        )
        transition(EntityType.RUN, run.status, RunState.RECOVERING, inbox)

        expired_attempt = replace(
            attempt,
            status=AttemptState.LOST,
            version=attempt.version + 1,
            updated_at=now,
            ended_at=now,
        )
        expired_lease = replace(
            lease,
            state=ExecutionLeaseState.EXPIRED,
            version=lease.version + 1,
            released_at=now,
            updated_at=now,
        )
        # Successor generation follows the reserve chain (next+1) so the
        # (unit, generation) uniqueness holds: the LOST attempt keeps its
        # generation, the successor moves one ahead.
        successor_generation = unit.next_generation + 1
        successor_attempt = AttemptRecord(
            tenant_id=ctx.tenant_id,
            attempt_id=store.new_id("attempt"),
            run_id=run.run_id,
            execution_unit_id=unit.execution_unit_id,
            step_id=attempt.step_id,
            generation=successor_generation,
            status=AttemptState.PROVISIONING,
            version=1,
            runtime_profile=unit.runtime_profile,
            source_checkpoint_id=source.checkpoint_id,
            reservation_key=f"recovery:{handler_version}:{message_id}",
            created_at=now,
            updated_at=now,
            started_at=None,
            ended_at=None,
            failure_id=None,
        )
        successor_lease = ExecutionLeaseRecord(
            tenant_id=ctx.tenant_id,
            lease_id=store.new_id("lease"),
            run_id=run.run_id,
            execution_unit_id=unit.execution_unit_id,
            attempt_id=successor_attempt.attempt_id,
            generation=successor_generation,
            state=ExecutionLeaseState.RESERVED,
            owner=None,
            version=1,
            activated_from_version=None,
            provision_deadline=now + provision_window,
            heartbeat_at=None,
            expires_at=None,
            released_at=None,
            created_at=now,
            updated_at=now,
        )
        recovering_unit = replace(
            unit,
            status=ExecutionUnitState.RECOVERING,
            next_generation=successor_generation,
            version=unit.version + 1,
            updated_at=now,
        )
        event_specs = (
            (
                EventType.ATTEMPT_LIFECYCLE,
                AttemptLifecyclePayload(
                    kind="attempt.lifecycle",
                    attempt_id=attempt.attempt_id,
                    status=AttemptState.LOST,
                ),
                "attempt-lifecycle/v1",
                attempt.attempt_id,
            ),
            (
                EventType.ATTEMPT_LIFECYCLE,
                AttemptLifecyclePayload(
                    kind="attempt.lifecycle",
                    attempt_id=successor_attempt.attempt_id,
                    status=AttemptState.PROVISIONING,
                ),
                "attempt-lifecycle/v1",
                successor_attempt.attempt_id,
            ),
            (
                EventType.RUN_STATUS_CHANGED,
                RunStatusChangedPayload(
                    kind="run.status.changed",
                    previous=run.status,
                    current=RunState.RECOVERING,
                ),
                "run-status/v1",
                successor_attempt.attempt_id,
            ),
        )
        events = tuple(
            EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=store.new_id("event"),
                tenant_id=ctx.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + offset,
                event_type=event_type,
                occurred_at=now,
                producer_service="control-plane",
                payload_schema=payload_schema,
                payload=payload,
                attempt_id=event_attempt_id,
                trace_id=ctx.trace_id,
            )
            for offset, (event_type, payload, payload_schema, event_attempt_id) in enumerate(
                event_specs, 1
            )
        )
        recovering_run = replace(
            run,
            status=RunState.RECOVERING,
            status_reason="LEASE_EXPIRED",
            version=run.version + 1,
            last_event_seq=events[-1].event_seq,
            updated_at=now,
        )
        audit = AuditEventRecord(
            tenant_id=ctx.tenant_id,
            audit_event_id=store.new_id("audit"),
            run_id=run.run_id,
            actor_id=ctx.actor_id,
            action="execution.lease.expired.recovered",
            entity_type="attempt",
            entity_id=successor_attempt.attempt_id,
            entity_version=successor_attempt.version,
            outcome="RECOVERING",
            trace_id=ctx.trace_id,
            details={
                "expired_attempt_id": attempt.attempt_id,
                "expired_generation": generation,
                "successor_generation": successor_generation,
                "source_checkpoint_id": source.checkpoint_id,
            },
            created_at=now,
        )
        outbox = (
            OutboxMessageRecord(
                tenant_id=ctx.tenant_id,
                message_id=store.new_id("outbox"),
                run_id=run.run_id,
                topic="attempt.provisioning.requested",
                payload={
                    "attempt_id": successor_attempt.attempt_id,
                    "execution_unit_id": unit.execution_unit_id,
                    "generation": successor_generation,
                    "source_checkpoint_id": source.checkpoint_id,
                },
                event_id=events[1].event_id,
                aggregate_version=recovering_run.version,
                created_at=now,
                published_at=None,
            ),
            OutboxMessageRecord(
                tenant_id=ctx.tenant_id,
                message_id=store.new_id("outbox"),
                run_id=run.run_id,
                topic="runtime.capability.revoke.requested",
                payload={"attempt_id": attempt.attempt_id, "generation": generation},
                event_id=events[0].event_id,
                aggregate_version=recovering_run.version,
                created_at=now,
                published_at=None,
            ),
            OutboxMessageRecord(
                tenant_id=ctx.tenant_id,
                message_id=store.new_id("outbox"),
                run_id=run.run_id,
                topic="sandbox.delete.requested",
                payload={"attempt_id": attempt.attempt_id, "generation": generation},
                event_id=events[0].event_id,
                aggregate_version=recovering_run.version,
                created_at=now,
                published_at=None,
            ),
        )

        await tx.replace_attempt_cas(expired_attempt, attempt.version)
        await tx.replace_lease_cas(expired_lease, lease.version)
        await tx.insert_attempt(successor_attempt)
        await tx.insert_lease(successor_lease)
        await tx.replace_execution_unit_cas(recovering_unit, unit.version)
        await tx.replace_run_cas(recovering_run, run.version)
        previous_seq = run.last_event_seq
        for event in events:
            await tx.append_event(event, previous_seq)
            previous_seq = event.event_seq
        await tx.insert_audit(audit)
        for message in outbox:
            await tx.insert_outbox(message)
        await tx.replace_inbox_message_cas(
            replace(
                inbox,
                processing_state="PROCESSED",
                version=inbox.version + 1,
                processed_at=now,
            ),
            inbox.version,
        )
        return RecoveryResult(
            expired_attempt=expired_attempt,
            expired_lease=expired_lease,
            successor_attempt=successor_attempt,
            successor_lease=successor_lease,
        )


async def recover_stale_provisioning(
    store: PlatformStore,
    ctx: RequestContext,
    *,
    attempt_id: str,
) -> RecoveryResult | None:
    """Phase 4.5 (4.4 leftover #2): sweep-driven recovery of Attempts orphaned
    in PROVISIONING whose RESERVED Lease has passed ``provision_deadline``.

    ``recover_expired_lease`` handles the runtime-side orphan (a LOST/expired
    ACTIVE Lease); this handles the reservation-side orphan: the Scheduler
    reserved the Attempt + created the Lease, then crashed before Job/Pod
    dispatch - no Runtime ever exists to expire them, so the Run/Unit stays
    blocked by the one-active-Attempt/Lease guard forever (observed in Phase
    4.4 after an orchestrator restart).

    Every check re-reads under a fresh transaction and CAS-fails fast on
    version, so a concurrently-activated Attempt makes this a no-op.
    """
    if not attempt_id:
        raise PlatformError("INTEGRITY_VIOLATION", "attempt_id is required")
    async with store.transaction() as tx:
        now = await tx.db_now()
        attempt = await tx.get_attempt(ctx.tenant_id, attempt_id)
        run = await tx.lock_run(ctx.tenant_id, attempt.run_id)
        unit = await tx.lock_execution_unit(ctx.tenant_id, attempt.execution_unit_id)
        lease = await tx.get_lease_for_attempt(ctx.tenant_id, attempt_id)
        if (
            attempt.run_id != run.run_id
            or attempt.execution_unit_id != unit.execution_unit_id
            or lease.run_id != run.run_id
            or lease.attempt_id != attempt.attempt_id
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "runtime facts are inconsistent")
        if attempt.status is not AttemptState.PROVISIONING:
            return None  # already progressed (concurrent activation) - nothing to do
        if lease.state is not ExecutionLeaseState.RESERVED:
            return None
        if lease.provision_deadline is None or now < lease.provision_deadline:
            return None  # not stale yet - the sweep retries on a later tick
        if unit.next_generation != attempt.generation:
            raise PlatformError("STALE_GENERATION", "ExecutionUnit generation has advanced")

        transition(EntityType.ATTEMPT, attempt.status, AttemptState.FAILED, None)
        transition(
            EntityType.EXECUTION_LEASE,
            lease.state,
            ExecutionLeaseState.RELEASED,
            None,
        )
        try:
            transition(
                EntityType.EXECUTION_UNIT,
                unit.status,
                ExecutionUnitState.RECOVERING,
                None,
            )
        except Exception:  # noqa: BLE001 - RECOVERING stays put (idempotent keep)
            if unit.status is not ExecutionUnitState.RECOVERING:
                raise
        # The Run may be QUEUED on a *first* attempt whose scheduler crashed
        # before dispatch (fsm forbids QUEUED->RECOVERING) or already
        # RECOVERING on a retry. Flip to RECOVERING only when legal; otherwise
        # keep the Run put and skip the RUN_STATUS_CHANGED event (no fake
        # transition in the event log).
        run_after_recovery = run.status
        emit_run_status_event = False
        try:
            transition(EntityType.RUN, run.status, RunState.RECOVERING, None)
            run_after_recovery = RunState.RECOVERING
            emit_run_status_event = True
        except Exception:  # noqa: BLE001 - QUEUED/RECOVERING stay as-is
            if run.status not in (RunState.QUEUED, RunState.RECOVERING):
                raise

        failed_attempt = replace(
            attempt,
            status=AttemptState.FAILED,
            version=attempt.version + 1,
            updated_at=now,
            ended_at=now,
        )
        released_lease = replace(
            lease,
            state=ExecutionLeaseState.RELEASED,
            version=lease.version + 1,
            released_at=now,
            updated_at=now,
        )
        # Successor generation follows the reserve chain (next+1): the FAILED
        # attempt keeps generation N, the successor takes N+1, and the Unit's
        # next_generation tracks it — same monotonic sequence a normal
        # re-reserve would produce, so (tenant, unit, generation) uniqueness
        # holds on both stores.
        successor_generation = unit.next_generation + 1
        successor_attempt = AttemptRecord(
            tenant_id=ctx.tenant_id,
            attempt_id=store.new_id("attempt"),
            run_id=run.run_id,
            execution_unit_id=unit.execution_unit_id,
            step_id=attempt.step_id,
            generation=successor_generation,
            status=AttemptState.PROVISIONING,
            version=1,
            runtime_profile=unit.runtime_profile,
            source_checkpoint_id=attempt.source_checkpoint_id,
            reservation_key=f"stale-provisioning:{attempt_id}",
            created_at=now,
            updated_at=now,
            started_at=None,
            ended_at=None,
            failure_id=None,
        )
        successor_lease = ExecutionLeaseRecord(
            tenant_id=ctx.tenant_id,
            lease_id=store.new_id("lease"),
            run_id=run.run_id,
            execution_unit_id=unit.execution_unit_id,
            attempt_id=successor_attempt.attempt_id,
            generation=successor_generation,
            state=ExecutionLeaseState.RESERVED,
            owner=None,
            version=1,
            activated_from_version=None,
            provision_deadline=now + timedelta(minutes=10),
            heartbeat_at=None,
            expires_at=None,
            released_at=None,
            created_at=now,
            updated_at=now,
        )
        recovering_unit = replace(
            unit,
            status=ExecutionUnitState.RECOVERING,
            next_generation=successor_generation,
            version=unit.version + 1,
            updated_at=now,
        )
        event_specs = [
            (
                EventType.ATTEMPT_LIFECYCLE,
                AttemptLifecyclePayload(
                    kind="attempt.lifecycle",
                    attempt_id=attempt.attempt_id,
                    status=AttemptState.FAILED,
                ),
                "attempt-lifecycle/v1",
                attempt.attempt_id,
            ),
            (
                EventType.ATTEMPT_LIFECYCLE,
                AttemptLifecyclePayload(
                    kind="attempt.lifecycle",
                    attempt_id=successor_attempt.attempt_id,
                    status=AttemptState.PROVISIONING,
                ),
                "attempt-lifecycle/v1",
                successor_attempt.attempt_id,
            ),
        ]
        if emit_run_status_event:
            event_specs.append(
                (
                    EventType.RUN_STATUS_CHANGED,
                    RunStatusChangedPayload(
                        kind="run.status.changed",
                        previous=run.status,
                        current=RunState.RECOVERING,
                    ),
                    "run-status/v1",
                    successor_attempt.attempt_id,
                )
            )
        event_specs = tuple(event_specs)
        events = tuple(
            EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=store.new_id("event"),
                tenant_id=ctx.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + offset,
                event_type=event_type,
                occurred_at=now,
                producer_service="control-plane",
                payload_schema=payload_schema,
                payload=payload,
                attempt_id=event_attempt_id,
                trace_id=ctx.trace_id,
            )
            for offset, (event_type, payload, payload_schema, event_attempt_id) in enumerate(
                event_specs, 1
            )
        )
        recovering_run = replace(
            run,
            status=run_after_recovery,
            status_reason=None,
            version=run.version + 1,
            last_event_seq=events[-1].event_seq,
            updated_at=now,
        )
        audit = AuditEventRecord(
            tenant_id=ctx.tenant_id,
            audit_event_id=store.new_id("audit"),
            run_id=run.run_id,
            actor_id=ctx.actor_id,
            action="execution.provisioning.recovered",
            entity_type="attempt",
            entity_id=successor_attempt.attempt_id,
            entity_version=successor_attempt.version,
            outcome="RECOVERING",
            trace_id=ctx.trace_id,
            details={
                "stale_attempt_id": attempt.attempt_id,
                "stale_generation": attempt.generation,
                "successor_generation": successor_generation,
                "source_checkpoint_id": attempt.source_checkpoint_id,
            },
            created_at=now,
        )
        outbox = OutboxMessageRecord(
            tenant_id=ctx.tenant_id,
            message_id=store.new_id("outbox"),
            run_id=run.run_id,
            topic="attempt.provisioning.requested",
            payload={
                "attempt_id": successor_attempt.attempt_id,
                "execution_unit_id": unit.execution_unit_id,
                "generation": successor_generation,
                "source_checkpoint_id": attempt.source_checkpoint_id,
            },
            event_id=events[1].event_id,
            aggregate_version=recovering_run.version,
            created_at=now,
            published_at=None,
        )

        await tx.replace_attempt_cas(failed_attempt, attempt.version)
        await tx.replace_lease_cas(released_lease, lease.version)
        await tx.insert_attempt(successor_attempt)
        await tx.insert_lease(successor_lease)
        await tx.replace_execution_unit_cas(recovering_unit, unit.version)
        await tx.replace_run_cas(recovering_run, run.version)
        previous_seq = run.last_event_seq
        for event in events:
            await tx.append_event(event, previous_seq)
            previous_seq = event.event_seq
        await tx.insert_audit(audit)
        await tx.insert_outbox(outbox)
        return RecoveryResult(
            expired_attempt=attempt,
            expired_lease=lease,
            successor_attempt=successor_attempt,
            successor_lease=successor_lease,
        )
