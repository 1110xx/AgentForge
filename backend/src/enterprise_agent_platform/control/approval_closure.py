"""Step-less approval closure for live attempt-pod runs (M6-C).

Live runs have no Step records (M6-C decision D6); the shared decision service
consumes the approval and moves Run/Unit to RECOVERING while the original
Attempt stays CHECKPOINTED_FOR_APPROVAL. After a succeeded Effect this module
re-opens the successor Attempt (reserve + activate, exactly like the Fair
Scheduler would for a round-2 pod, but without dispatching a Pod) and finishes
the Run SUCCEEDED inside one transaction — mirroring the reference terminal
adapter minus the Step layer.
"""
from __future__ import annotations

from dataclasses import replace

from enterprise_agent_platform.contracts.enums import (
    AttemptState,
    EffectState,
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
from enterprise_agent_platform.domain.fsm import transition
from enterprise_agent_platform.domain.records import (
    AuditEventRecord,
    OutboxMessageRecord,
)
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore

from .context import RequestContext
from .service import ControlPlaneService


async def close_run_after_effect(
    store: PlatformStore,
    ctx: RequestContext,
    *,
    run_id: str,
    effect_id: str,
    executor_actor: str = "approval-closure",
    transition_key: str | None = None,
) -> None:
    """Finish a live Run after its approved Effect reached SUCCEEDED.

    Idempotent: a Run that already reached SUCCEEDED returns immediately; an
    Effect that is still PREPARED is left untouched (the caller decides whether
    to execute it). Raises when the Effect is not terminal or the Run is not in
    the recoverable state the closure expects.
    """
    tenant_id = ctx.tenant_id
    effect = await store.get_effect(tenant_id, effect_id)
    run = await store.get_run(tenant_id, run_id)
    if run.status is RunState.SUCCEEDED:
        return
    if effect.state is not EffectState.SUCCEEDED:
        raise PlatformError(
            "EFFECT_NOT_SUCCEEDED", "only a succeeded Effect can finish the Run"
        )
    proposal = await store.get_action_proposal(tenant_id, effect.action_ref)
    unit = await store.get_execution_unit(tenant_id, proposal.execution_unit_id)
    if (
        run.status is not RunState.RECOVERING
        or unit.status is not ExecutionUnitState.RECOVERING
    ):
        raise PlatformError(
            "INVALID_STATE", "succeeded Effect did not resume checkpoint work"
        )

    # Reopen the successor Attempt (no Pod is dispatched for it): the reserved
    # + activated Attempt/Lease satisfy the same state machine the terminal
    # transition expects and keep the record chain (generation+1) honest.
    closure_ctx = replace(
        ctx,
        actor_id=executor_actor,
        scopes=tuple(sorted(set(ctx.scopes) | {"runs:execute"})),
    )
    control = ControlPlaneService(store)
    reservation = await control.reserve_attempt(
        closure_ctx,
        unit.execution_unit_id,
        unit.current_checkpoint_id or "",
        unit.version,
        transition_key=transition_key or f"approval-closure:{effect_id}",
    )
    await control.activate_lease(
        closure_ctx,
        reservation.attempt.attempt_id,
        reservation.attempt.generation,
        executor_actor,
        reservation.lease.version,
    )

    attempt = await store.get_attempt(tenant_id, reservation.attempt.attempt_id)
    run = await store.get_run(tenant_id, run_id)
    unit = await store.get_execution_unit(tenant_id, unit.execution_unit_id)
    async with store.transaction() as tx:
        now = await tx.db_now()
        locked_run = await tx.lock_run(tenant_id, run.run_id)
        locked_unit = await tx.lock_execution_unit(tenant_id, unit.execution_unit_id)
        current_attempt = await tx.get_attempt(tenant_id, attempt.attempt_id)
        current_lease = await tx.get_lease_for_attempt(tenant_id, attempt.attempt_id)
        if (
            locked_run.status is not RunState.RUNNING
            or locked_unit.status is not ExecutionUnitState.EXECUTING
            or current_attempt.status is not AttemptState.CLAIMED
            or current_lease.state is not ExecutionLeaseState.ACTIVE
        ):
            raise PlatformError("INVALID_STATE", "successor Runtime is not ready to finish")

        transition(EntityType.ATTEMPT, current_attempt.status, AttemptState.RUNNING, effect)
        running_attempt = replace(
            current_attempt,
            status=AttemptState.RUNNING,
            version=current_attempt.version + 1,
            updated_at=now,
        )
        await tx.replace_attempt_cas(running_attempt, current_attempt.version)
        transition(EntityType.ATTEMPT, running_attempt.status, AttemptState.SUCCEEDED, effect)
        transition(
            EntityType.EXECUTION_LEASE,
            current_lease.state,
            ExecutionLeaseState.RELEASED,
            effect,
        )
        transition(
            EntityType.EXECUTION_UNIT,
            locked_unit.status,
            ExecutionUnitState.SUCCEEDED,
            effect,
        )
        transition(EntityType.RUN, locked_run.status, RunState.SUCCEEDED, effect)

        succeeded_attempt = replace(
            running_attempt,
            status=AttemptState.SUCCEEDED,
            version=running_attempt.version + 1,
            updated_at=now,
            ended_at=now,
        )
        released_lease = replace(
            current_lease,
            state=ExecutionLeaseState.RELEASED,
            version=current_lease.version + 1,
            released_at=now,
            updated_at=now,
        )
        succeeded_unit = replace(
            locked_unit,
            status=ExecutionUnitState.SUCCEEDED,
            version=locked_unit.version + 1,
            updated_at=now,
        )
        attempt_event = EnterpriseEventEnvelope(
            schema_version="enterprise-event/v1",
            event_id=store.new_id("event"),
            tenant_id=tenant_id,
            run_id=run.run_id,
            event_seq=locked_run.last_event_seq + 1,
            event_type=EventType.ATTEMPT_LIFECYCLE,
            occurred_at=now,
            producer_service="approval-closure",
            payload_schema="attempt-lifecycle/v1",
            payload=AttemptLifecyclePayload(
                kind="attempt.lifecycle",
                attempt_id=attempt.attempt_id,
                status=AttemptState.SUCCEEDED,
            ),
            attempt_id=attempt.attempt_id,
            trace_id=ctx.trace_id,
        )
        run_event = EnterpriseEventEnvelope(
            schema_version="enterprise-event/v1",
            event_id=store.new_id("event"),
            tenant_id=tenant_id,
            run_id=run.run_id,
            event_seq=attempt_event.event_seq + 1,
            event_type=EventType.RUN_STATUS_CHANGED,
            occurred_at=now,
            producer_service="approval-closure",
            payload_schema="run-status/v1",
            payload=RunStatusChangedPayload(
                kind="run.status.changed",
                previous=locked_run.status,
                current=RunState.SUCCEEDED,
            ),
            attempt_id=attempt.attempt_id,
            causation_event_id=attempt_event.event_id,
            trace_id=ctx.trace_id,
        )
        succeeded_run = replace(
            locked_run,
            status=RunState.SUCCEEDED,
            status_reason=None,
            version=locked_run.version + 1,
            last_event_seq=run_event.event_seq,
            updated_at=now,
            ended_at=now,
        )
        await tx.replace_attempt_cas(succeeded_attempt, running_attempt.version)
        await tx.replace_lease_cas(released_lease, current_lease.version)
        await tx.replace_execution_unit_cas(succeeded_unit, locked_unit.version)
        await tx.replace_run_cas(succeeded_run, locked_run.version)
        await tx.append_event(attempt_event, locked_run.last_event_seq)
        await tx.append_event(run_event, attempt_event.event_seq)
        await tx.insert_audit(
            AuditEventRecord(
                tenant_id=tenant_id,
                audit_event_id=store.new_id("audit"),
                run_id=run.run_id,
                actor_id=executor_actor,
                action="run.succeeded.approval_closure",
                entity_type="run",
                entity_id=run.run_id,
                entity_version=succeeded_run.version,
                outcome="SUCCEEDED",
                trace_id=ctx.trace_id,
                details={"effect_id": effect.effect_id},
                created_at=now,
            )
        )
        await tx.insert_outbox(
            OutboxMessageRecord(
                tenant_id=tenant_id,
                message_id=store.new_id("outbox"),
                run_id=run.run_id,
                topic="run.terminal",
                payload={"run_id": run.run_id},
                event_id=run_event.event_id,
                aggregate_version=succeeded_run.version,
                created_at=now,
                published_at=None,
            )
        )


__all__ = ["close_run_after_effect"]
