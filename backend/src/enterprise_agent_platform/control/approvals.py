"""Durable approval decisions and idempotent Effect preparation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Literal

from enterprise_agent_platform.contracts.enums import (
    ActionProposalState,
    ApprovalState,
    EffectState,
    EntityType,
    EventType,
    ExecutionUnitState,
    RunState,
    StepState,
)
from enterprise_agent_platform.contracts.events import (
    ApprovalDecidedPayload,
    EnterpriseEventEnvelope,
    RunStatusChangedPayload,
)
from enterprise_agent_platform.domain.action_digest import compute_effect_key
from enterprise_agent_platform.domain.fsm import transition
from enterprise_agent_platform.domain.records import (
    AuditEventRecord,
    EffectLedgerRecord,
    OutboxMessageRecord,
)
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore

from .context import RequestContext


def _decision_digest(
    ctx: RequestContext,
    *,
    approval_id: str,
    decision: str,
    displayed_digest: str,
    client_action_id: str,
) -> str:
    canonical = json.dumps(
        {
            "actor_id": ctx.actor_id,
            "approval_id": approval_id,
            "client_action_id": client_action_id,
            "decision": decision,
            "displayed_digest": displayed_digest,
            "tenant_id": ctx.tenant_id,
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ApprovalDecisionService:
    """Consume a bound approval once and durably enqueue its external Effect."""

    def __init__(self, store: PlatformStore) -> None:
        self._store = store

    async def decide(
        self,
        ctx: RequestContext,
        *,
        approval_id: str,
        decision: Literal["APPROVE", "REJECT"],
        displayed_digest: str,
        client_action_id: str,
        idempotency_key: str,
    ) -> None:
        if "approvals:decide" not in ctx.scopes:
            raise PlatformError("FORBIDDEN", "approval decision scope is required")
        if not all((approval_id, displayed_digest, client_action_id, idempotency_key)):
            raise PlatformError("INTEGRITY_VIOLATION", "approval decision identity is required")
        if client_action_id != idempotency_key:
            raise PlatformError(
                "REQUEST_VALIDATION_FAILED",
                "client action id must equal the idempotency key",
            )
        if decision not in {"APPROVE", "REJECT"}:
            raise PlatformError("REQUEST_VALIDATION_FAILED", "decision is invalid")
        request_digest = _decision_digest(
            ctx,
            approval_id=approval_id,
            decision=decision,
            displayed_digest=displayed_digest,
            client_action_id=client_action_id,
        )
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            existing = await tx.claim_idempotency(
                ctx.tenant_id,
                "approval_decision",
                idempotency_key,
                request_digest,
                ctx.actor_id,
                now,
            )
            if existing is not None:
                if (
                    existing.status != "COMPLETED"
                    or existing.result_type != "approval_decision"
                    or existing.result_schema != "approval-decision/v1"
                    or existing.result_id != approval_id
                ):
                    raise PlatformError(
                        "IDEMPOTENCY_IN_PROGRESS",
                        "approval decision is still being committed",
                        retryable=True,
                    )
                return

            approval = await tx.get_approval_request(ctx.tenant_id, approval_id)
            proposal = await tx.get_action_proposal(ctx.tenant_id, approval.action_ref)
            run = await tx.lock_run(ctx.tenant_id, approval.run_id)
            unit = await tx.lock_execution_unit(ctx.tenant_id, proposal.execution_unit_id)
            step = (
                None
                if approval.step_id is None
                else await tx.get_step(ctx.tenant_id, approval.step_id)
            )
            if (
                approval.status is not ApprovalState.PENDING
                or approval.expires_at <= now
                or approval.request_digest != displayed_digest
                or proposal.status is not ActionProposalState.OPEN
                or proposal.run_id != approval.run_id
                or proposal.step_id != approval.step_id
                or proposal.request_digest != approval.request_digest
                or proposal.payload_ref != approval.canonical_request_ref
                or unit.run_id != run.run_id
                or run.status is not RunState.WAITING_APPROVAL
                or unit.status is not ExecutionUnitState.WAITING_APPROVAL
                or step is None
                or step.run_id != run.run_id
                or step.status is not StepState.WAITING_APPROVAL
            ):
                raise PlatformError(
                    "APPROVAL_DECISION_REJECTED",
                    "approval is stale, expired, or no longer bound to waiting work",
                )

            target_approval_state = (
                ApprovalState.APPROVED if decision == "APPROVE" else ApprovalState.REJECTED
            )
            target_proposal_state = (
                ActionProposalState.CONSUMED
                if decision == "APPROVE"
                else ActionProposalState.REJECTED
            )
            transition(
                EntityType.APPROVAL,
                approval.status,
                target_approval_state,
                decision,
            )
            transition(
                EntityType.ACTION_PROPOSAL,
                proposal.status,
                target_proposal_state,
                decision,
            )
            decided_approval = replace(
                approval,
                status=target_approval_state,
                version=approval.version + 1,
                decided_by=ctx.actor_id,
                decided_at=now,
                decision_reason=("USER_APPROVED" if decision == "APPROVE" else "USER_REJECTED"),
                updated_at=now,
            )
            consumed_proposal = replace(
                proposal,
                status=target_proposal_state,
                version=proposal.version + 1,
            )
            approval_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self._store.new_id("event"),
                tenant_id=ctx.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 1,
                event_type=EventType.APPROVAL_DECIDED,
                occurred_at=now,
                producer_service="control-plane",
                payload_schema="approval/v1",
                payload=ApprovalDecidedPayload(
                    kind="approval.decided",
                    approval_id=approval.approval_id,
                    status=target_approval_state,
                ),
                attempt_id=proposal.attempt_id,
                causation_event_id=None,
                trace_id=ctx.trace_id,
            )
            last_event = approval_event
            updated_run = replace(
                run,
                version=run.version + 1,
                last_event_seq=approval_event.event_seq,
                updated_at=now,
            )
            effect: EffectLedgerRecord | None = None
            outbox: list[OutboxMessageRecord] = []
            if decision == "APPROVE":
                effect_key = compute_effect_key(
                    tenant_id=ctx.tenant_id,
                    run_id=run.run_id,
                    action_ref=proposal.action_ref,
                    request_digest=proposal.request_digest,
                )
                if await tx.get_effect_by_key(ctx.tenant_id, effect_key) is not None:
                    raise PlatformError(
                        "EFFECT_ALREADY_PREPARED",
                        "an Effect already exists for this approved proposal",
                    )
                effect = EffectLedgerRecord(
                    tenant_id=ctx.tenant_id,
                    effect_id=self._store.new_id("effect"),
                    run_id=run.run_id,
                    action_ref=proposal.action_ref,
                    approval_id=approval.approval_id,
                    effect_key=effect_key,
                    request_digest=proposal.request_digest,
                    tool_name=proposal.tool_name,
                    tool_version=proposal.tool_spec_version,
                    tool_spec_digest=proposal.tool_spec_digest,
                    connector_name=proposal.connector_name,
                    required_scopes=proposal.required_scopes,
                    canonical_target=proposal.canonical_target,
                    canonical_payload_digest=proposal.canonical_payload_digest,
                    state=EffectState.PREPARED,
                    version=1,
                    executor_id=None,
                    execution_epoch=0,
                    executor_lease_expires_at=None,
                    result_ref=None,
                    remote_operation_id=None,
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
                outbox.append(
                    OutboxMessageRecord(
                        tenant_id=ctx.tenant_id,
                        message_id=self._store.new_id("outbox"),
                        run_id=run.run_id,
                        topic="effect.execute.requested",
                        payload={"effect_id": effect.effect_id},
                        event_id=approval_event.event_id,
                        aggregate_version=updated_run.version,
                        created_at=now,
                        published_at=None,
                    )
                )
            else:
                pass
            transition(EntityType.STEP, step.status, StepState.ACTIVE, decision)
            transition(
                EntityType.EXECUTION_UNIT,
                unit.status,
                ExecutionUnitState.RECOVERING,
                decision,
            )
            transition(EntityType.RUN, run.status, RunState.RECOVERING, decision)
            run_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self._store.new_id("event"),
                tenant_id=ctx.tenant_id,
                run_id=run.run_id,
                event_seq=approval_event.event_seq + 1,
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
                causation_event_id=approval_event.event_id,
                trace_id=ctx.trace_id,
            )
            last_event = run_event
            status_reason = (
                "APPROVAL_APPROVED" if decision == "APPROVE" else "APPROVAL_REJECTED"
            )
            updated_run = replace(
                updated_run,
                status=RunState.RECOVERING,
                status_reason=status_reason,
                last_event_seq=run_event.event_seq,
            )
            recovering_unit = replace(
                unit,
                status=ExecutionUnitState.RECOVERING,
                version=unit.version + 1,
                updated_at=now,
            )
            active_step = replace(
                step,
                status=StepState.ACTIVE,
                status_reason=status_reason,
                version=step.version + 1,
                updated_at=now,
            )
            await tx.replace_step_cas(active_step, step.version)
            await tx.replace_execution_unit_cas(recovering_unit, unit.version)
            if decision == "REJECT":
                outbox.append(
                    OutboxMessageRecord(
                        tenant_id=ctx.tenant_id,
                        message_id=self._store.new_id("outbox"),
                        run_id=run.run_id,
                        topic="scheduler.work.ready",
                        payload={
                            "execution_unit_id": unit.execution_unit_id,
                            "checkpoint_id": unit.current_checkpoint_id,
                        },
                        event_id=run_event.event_id,
                        aggregate_version=updated_run.version,
                        created_at=now,
                        published_at=None,
                    )
                )

            await tx.replace_approval_request_cas(decided_approval, approval.version)
            await tx.replace_action_proposal_cas(consumed_proposal, proposal.version)
            if effect is not None:
                await tx.insert_effect(effect)
            await tx.replace_run_cas(updated_run, run.version)
            await tx.append_event(approval_event, run.last_event_seq)
            if last_event is not approval_event:
                await tx.append_event(last_event, approval_event.event_seq)
            for message in outbox:
                await tx.insert_outbox(message)
            await tx.insert_audit(
                AuditEventRecord(
                    tenant_id=ctx.tenant_id,
                    audit_event_id=self._store.new_id("audit"),
                    run_id=run.run_id,
                    actor_id=ctx.actor_id,
                    action="approval.decided",
                    entity_type="approval",
                    entity_id=approval.approval_id,
                    entity_version=decided_approval.version,
                    outcome=target_approval_state.value,
                    trace_id=ctx.trace_id,
                    details={
                        "action_ref": proposal.action_ref,
                        "client_action_id": client_action_id,
                        "effect_id": None if effect is None else effect.effect_id,
                        "request_digest": approval.request_digest,
                    },
                    created_at=now,
                )
            )
            await tx.complete_idempotency(
                ctx.tenant_id,
                "approval_decision",
                idempotency_key,
                request_digest,
                "approval_decision",
                approval.approval_id,
                "approval-decision/v1",
                {
                    "approval_id": approval.approval_id,
                    "client_action_id": client_action_id,
                    "decision": decision,
                    "effect_id": None if effect is None else effect.effect_id,
                    "event_id": last_event.event_id,
                    "run_id": run.run_id,
                },
                now,
            )


__all__ = ["ApprovalDecisionService"]
