from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from enterprise_agent_platform.contracts.enums import (
    EffectState,
    EntityType,
    EventType,
    ExecutionUnitState,
    RunState,
    StepState,
)
from enterprise_agent_platform.contracts.events import (
    EnterpriseEventEnvelope,
    RunStatusChangedPayload,
)
from enterprise_agent_platform.domain.fsm import transition
from enterprise_agent_platform.domain.records import AuditEventRecord, OutboxMessageRecord
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore

from .context import RequestContext


@dataclass(frozen=True, slots=True)
class FailedEffectRecovery:
    effect_id: str
    run_id: str
    run_version: int
    run_status: RunState


def _request_digest(
    ctx: RequestContext,
    *,
    run_id: str,
    effect_id: str,
    expected_run_version: int,
) -> str:
    canonical = json.dumps(
        {
            "actor_id": ctx.actor_id,
            "effect_id": effect_id,
            "expected_run_version": expected_run_version,
            "run_id": run_id,
            "tenant_id": ctx.tenant_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class FailedEffectRecoveryService:
    """Resume checkpoint work so a new Attempt can replan after a FAILED Effect.

    The failed Effect is immutable and is never redispatched. The successor Agent
    must create a new proposal and obtain a new approval before another WRITE.
    """

    def __init__(self, store: PlatformStore) -> None:
        self._store = store

    async def recover(
        self,
        ctx: RequestContext,
        *,
        run_id: str,
        effect_id: str,
        expected_run_version: int,
        idempotency_key: str,
    ) -> FailedEffectRecovery:
        if "effects:recover" not in ctx.scopes:
            raise PlatformError("FORBIDDEN", "failed Effect recovery scope is required")
        if not idempotency_key:
            raise PlatformError("INTEGRITY_VIOLATION", "idempotency key is required")
        digest = _request_digest(
            ctx,
            run_id=run_id,
            effect_id=effect_id,
            expected_run_version=expected_run_version,
        )
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            existing = await tx.claim_idempotency(
                ctx.tenant_id,
                "failed_effect_recovery",
                idempotency_key,
                digest,
                ctx.actor_id,
                now,
            )
            if existing is not None:
                payload = existing.result_payload
                if (
                    existing.status != "COMPLETED"
                    or existing.result_type != "effect_recovery"
                    or existing.result_schema != "effect-recovery/v1"
                    or existing.result_id != effect_id
                    or payload is None
                ):
                    raise PlatformError(
                        "IDEMPOTENCY_IN_PROGRESS",
                        "failed Effect recovery is still being committed",
                        retryable=True,
                    )
                return FailedEffectRecovery(
                    effect_id=str(payload["effect_id"]),
                    run_id=str(payload["run_id"]),
                    run_version=int(payload["run_version"]),
                    run_status=RunState(str(payload["run_status"])),
                )

            effect = await tx.get_effect(ctx.tenant_id, effect_id)
            if effect.run_id != run_id:
                raise PlatformError("NOT_FOUND", "Effect was not found")
            proposal = await tx.get_action_proposal(ctx.tenant_id, effect.action_ref)
            run = await tx.lock_run(ctx.tenant_id, run_id)
            unit = await tx.lock_execution_unit(ctx.tenant_id, proposal.execution_unit_id)
            step = (
                None
                if proposal.step_id is None
                else await tx.get_step(ctx.tenant_id, proposal.step_id)
            )
            if run.version != expected_run_version:
                raise PlatformError("VERSION_CONFLICT", "run version compare-and-swap failed")
            if (
                effect.state is not EffectState.FAILED
                or run.status is not RunState.NEEDS_ATTENTION
                or unit.status is not ExecutionUnitState.NEEDS_ATTENTION
                or step is None
                or step.status is not StepState.NEEDS_ATTENTION
                or unit.current_checkpoint_id is None
            ):
                raise PlatformError(
                    "EFFECT_RECOVERY_STATE_INVALID",
                    "failed Effect is not bound to recoverable checkpoint work",
                )

            transition(
                EntityType.EXECUTION_UNIT,
                unit.status,
                ExecutionUnitState.RECOVERING,
                effect,
            )
            transition(EntityType.STEP, step.status, StepState.ACTIVE, effect)
            transition(EntityType.RUN, run.status, RunState.RECOVERING, effect)

            recovered_unit = replace(
                unit,
                status=ExecutionUnitState.RECOVERING,
                version=unit.version + 1,
                updated_at=now,
            )
            recovered_step = replace(
                step,
                status=StepState.ACTIVE,
                status_reason="FAILED_EFFECT_REPLAN_REQUESTED",
                version=step.version + 1,
                updated_at=now,
            )
            event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self._store.new_id("event"),
                tenant_id=ctx.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 1,
                event_type=EventType.RUN_STATUS_CHANGED,
                occurred_at=now,
                producer_service="control-plane",
                payload_schema="run-status/v1",
                payload=RunStatusChangedPayload(
                    kind="run.status.changed",
                    previous=run.status,
                    current=RunState.RECOVERING,
                ),
                attempt_id=proposal.attempt_id,
                trace_id=ctx.trace_id,
            )
            recovered_run = replace(
                run,
                status=RunState.RECOVERING,
                status_reason="FAILED_EFFECT_REPLAN_REQUESTED",
                version=run.version + 1,
                last_event_seq=event.event_seq,
                updated_at=now,
            )
            result = FailedEffectRecovery(
                effect_id=effect.effect_id,
                run_id=run.run_id,
                run_version=recovered_run.version,
                run_status=recovered_run.status,
            )
            result_payload = {
                "effect_id": result.effect_id,
                "run_id": result.run_id,
                "run_version": result.run_version,
                "run_status": result.run_status.value,
            }

            await tx.replace_execution_unit_cas(recovered_unit, unit.version)
            await tx.replace_step_cas(recovered_step, step.version)
            await tx.replace_run_cas(recovered_run, run.version)
            await tx.append_event(event, run.last_event_seq)
            await tx.insert_audit(
                AuditEventRecord(
                    tenant_id=ctx.tenant_id,
                    audit_event_id=self._store.new_id("audit"),
                    run_id=run.run_id,
                    actor_id=ctx.actor_id,
                    action="effect.failed.recovery.requested",
                    entity_type="effect",
                    entity_id=effect.effect_id,
                    entity_version=effect.version,
                    outcome="REPLAN_FROM_CHECKPOINT",
                    trace_id=ctx.trace_id,
                    details={"checkpoint_id": unit.current_checkpoint_id},
                    created_at=now,
                )
            )
            await tx.insert_outbox(
                OutboxMessageRecord(
                    tenant_id=ctx.tenant_id,
                    message_id=self._store.new_id("outbox"),
                    run_id=run.run_id,
                    topic="scheduler.work.ready",
                    payload={
                        "checkpoint_id": unit.current_checkpoint_id,
                        "effect_id": effect.effect_id,
                        "execution_unit_id": unit.execution_unit_id,
                    },
                    event_id=event.event_id,
                    aggregate_version=recovered_run.version,
                    created_at=now,
                    published_at=None,
                )
            )
            await tx.complete_idempotency(
                ctx.tenant_id,
                "failed_effect_recovery",
                idempotency_key,
                digest,
                "effect_recovery",
                effect.effect_id,
                "effect-recovery/v1",
                result_payload,
                now,
            )
            return result


__all__ = ["FailedEffectRecovery", "FailedEffectRecoveryService"]
