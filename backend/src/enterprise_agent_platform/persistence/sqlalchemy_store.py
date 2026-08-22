"""SQLAlchemy 2 async implementation of the Task Z persistence surface."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import fields, replace
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import uuid4

from sqlalchemy import Select, delete, exists, func, insert, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import TableClause

from enterprise_agent_platform.contracts.enums import (
    ActionProposalState,
    ApprovalState,
    ArtifactVersionState,
    AttemptState,
    CheckpointState,
    EffectState,
    ExecutionLeaseState,
    ExecutionUnitState,
    RunState,
    StepState,
    WorkspaceSnapshotState,
)
from enterprise_agent_platform.contracts.events import EnterpriseEventEnvelope
from enterprise_agent_platform.domain.records import (
    IDEMPOTENCY_RETENTION,
    ActionProposalRecord,
    ApprovalRequestRecord,
    ArtifactRecord,
    ArtifactVersionRecord,
    AttemptRecord,
    AuditEventRecord,
    CheckpointRecord,
    EffectLedgerRecord,
    ExecutionLeaseRecord,
    ExecutionUnitRecord,
    FollowupRequestRecord,
    IdempotencyRecord,
    InboxMessageRecord,
    OutboxMessageRecord,
    RunAuthorizationSnapshotRecord,
    RunRecord,
    SchedulableWork,
    StepRecord,
    UiSurfaceRecord,
    UiSurfaceRevisionRecord,
    WorkspaceSnapshotRecord,
)

from .invariants import (
    validate_effect_update,
    validate_new_action_proposal,
    validate_new_effect,
    validate_new_outbox,
)
from .protocol import PlatformError, PlatformTransaction
from .tables import (
    action_proposal_table,
    approval_request_table,
    artifact_table,
    artifact_version_table,
    attempt_table,
    audit_event_table,
    checkpoint_table,
    effect_ledger_table,
    execution_lease_table,
    execution_unit_table,
    followup_request_table,
    idempotency_record_table,
    inbox_message_table,
    outbox_message_table,
    run_authorization_snapshot_table,
    run_event_table,
    run_table,
    step_table,
    ui_surface_revision_table,
    ui_surface_table,
    workspace_snapshot_table,
)

ACTIVE_ATTEMPT_STATES = tuple(
    state.value
    for state in (
        AttemptState.CREATED,
        AttemptState.PROVISIONING,
        AttemptState.CLAIMED,
        AttemptState.RUNNING,
        AttemptState.CHECKPOINTING,
    )
)

ACTIVE_LEASE_STATES = (
    ExecutionLeaseState.RESERVED.value,
    ExecutionLeaseState.ACTIVE.value,
)

IDEMPOTENCY_RESULT_SCHEMAS = {
    "approval_decision": "approval-decision/v1",
    "effect_recovery": "effect-recovery/v1",
    "run": "run-record/v1",
    "attempt_reservation": "attempt-reservation/v1",
}

RecordT = TypeVar("RecordT")


def _not_found(entity: str) -> PlatformError:
    return PlatformError("NOT_FOUND", f"{entity} was not found")


def _aware(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _canonical_json(value: dict[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _record_values(record: Any) -> dict[str, object]:
    return {f.name: getattr(record, f.name) for f in fields(record)}


def _run(row: Any) -> RunRecord:
    return RunRecord(
        tenant_id=row._mapping["tenant_id"],
        run_id=row._mapping["run_id"],
        owner_id=row._mapping["owner_id"],
        parent_run_id=row._mapping["parent_run_id"],
        workflow_type=row._mapping["workflow_type"],
        intent=row._mapping["intent"],
        resource_refs=tuple(row._mapping["resource_refs"]),
        parameters=_canonical_json(row._mapping["parameters"]),
        host_context_ref=row._mapping["host_context_ref"],
        status=RunState(row._mapping["status"]),
        status_reason=row._mapping["status_reason"],
        version=row._mapping["version"],
        last_event_seq=row._mapping["last_event_seq"],
        fsm_version=row._mapping["fsm_version"],
        cancel_requested_by=row._mapping["cancel_requested_by"],
        cancel_requested_at=_aware(row._mapping["cancel_requested_at"]),
        cancel_reason=row._mapping["cancel_reason"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        updated_at=_aware(row._mapping["updated_at"]),  # type: ignore[arg-type]
        ended_at=_aware(row._mapping["ended_at"]),
    )


def _authorization_snapshot(row: Any) -> RunAuthorizationSnapshotRecord:
    return RunAuthorizationSnapshotRecord(
        tenant_id=row._mapping["tenant_id"],
        run_id=row._mapping["run_id"],
        resolved_resources=tuple(_canonical_json(item) for item in row._mapping["resolved_resources"]),
        host_context_digest=row._mapping["host_context_digest"],
        host_context_version=row._mapping["host_context_version"],
        policy_digest=row._mapping["policy_digest"],
        policy_version=row._mapping["policy_version"],
        policy_scopes=tuple(row._mapping["policy_scopes"]),
        policy_budget=_canonical_json(row._mapping["policy_budget"]),
        snapshot_digest=row._mapping["snapshot_digest"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
    )


def _unit(row: Any) -> ExecutionUnitRecord:
    return ExecutionUnitRecord(
        tenant_id=row._mapping["tenant_id"],
        execution_unit_id=row._mapping["execution_unit_id"],
        run_id=row._mapping["run_id"],
        role=row._mapping["role"],
        status=ExecutionUnitState(row._mapping["status"]),
        version=row._mapping["version"],
        current_checkpoint_id=row._mapping["current_checkpoint_id"],
        next_generation=row._mapping["next_generation"],
        runtime_profile=row._mapping["runtime_profile"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        updated_at=_aware(row._mapping["updated_at"]),  # type: ignore[arg-type]
    )


def _checkpoint(row: Any) -> CheckpointRecord:
    return CheckpointRecord(
        tenant_id=row._mapping["tenant_id"],
        checkpoint_id=row._mapping["checkpoint_id"],
        run_id=row._mapping["run_id"],
        execution_unit_id=row._mapping["execution_unit_id"],
        source_attempt_id=row._mapping["source_attempt_id"],
        checkpoint_seq=row._mapping["checkpoint_seq"],
        state=CheckpointState(row._mapping["state"]),
        workflow_cursor=_canonical_json(row._mapping["workflow_cursor"]),
        last_event_seq=row._mapping["last_event_seq"],
        workspace_snapshot_id=row._mapping["workspace_snapshot_id"],
        checkpoint_schema_version=row._mapping["checkpoint_schema_version"],
        runtime_profile_version=row._mapping["runtime_profile_version"],
        policy_version=row._mapping["policy_version"],
        tool_catalog_version=row._mapping["tool_catalog_version"],
        ui_catalog_version=row._mapping["ui_catalog_version"],
        checksum=row._mapping["checksum"],
        version=row._mapping["version"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        committed_at=_aware(row._mapping["committed_at"]),
        completed_step_ids=tuple(row._mapping["completed_step_ids"]),
        active_step_context=_canonical_json(row._mapping["active_step_context"]),
        input_artifact_versions=tuple(
            _canonical_json(item) for item in row._mapping["input_artifact_versions"]
        ),
        output_artifact_versions=tuple(
            _canonical_json(item) for item in row._mapping["output_artifact_versions"]
        ),
        resolved_tool_call_ids=tuple(row._mapping["resolved_tool_call_ids"]),
        effect_states=_canonical_json(row._mapping["effect_states"]),
        budget_consumed=_canonical_json(row._mapping["budget_consumed"]),
        model_context_summary_ref=row._mapping["model_context_summary_ref"],
        runtime_image_digest=row._mapping["runtime_image_digest"],
        agent_state=_canonical_json(row._mapping["agent_state"]),
        agent_state_schema_version=row._mapping["agent_state_schema_version"],
    )


def _step(row: Any) -> StepRecord:
    return StepRecord(
        tenant_id=row._mapping["tenant_id"],
        step_id=row._mapping["step_id"],
        run_id=row._mapping["run_id"],
        ordinal=row._mapping["ordinal"],
        name=row._mapping["name"],
        step_type=row._mapping["step_type"],
        policy_snapshot=_canonical_json(row._mapping["policy_snapshot"]),
        status=StepState(row._mapping["status"]),
        status_reason=row._mapping["status_reason"],
        version=row._mapping["version"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        updated_at=_aware(row._mapping["updated_at"]),  # type: ignore[arg-type]
        ended_at=_aware(row._mapping["ended_at"]),
    )


def _attempt(row: Any) -> AttemptRecord:
    return AttemptRecord(
        tenant_id=row._mapping["tenant_id"],
        attempt_id=row._mapping["attempt_id"],
        run_id=row._mapping["run_id"],
        execution_unit_id=row._mapping["execution_unit_id"],
        step_id=row._mapping["step_id"],
        generation=row._mapping["generation"],
        status=AttemptState(row._mapping["status"]),
        version=row._mapping["version"],
        runtime_profile=row._mapping["runtime_profile"],
        source_checkpoint_id=row._mapping["source_checkpoint_id"],
        reservation_key=row._mapping["reservation_key"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        updated_at=_aware(row._mapping["updated_at"]),  # type: ignore[arg-type]
        started_at=_aware(row._mapping["started_at"]),
        ended_at=_aware(row._mapping["ended_at"]),
        failure_id=row._mapping["failure_id"],
    )


def _lease(row: Any) -> ExecutionLeaseRecord:
    return ExecutionLeaseRecord(
        tenant_id=row._mapping["tenant_id"],
        lease_id=row._mapping["lease_id"],
        run_id=row._mapping["run_id"],
        execution_unit_id=row._mapping["execution_unit_id"],
        attempt_id=row._mapping["attempt_id"],
        generation=row._mapping["generation"],
        state=ExecutionLeaseState(row._mapping["state"]),
        owner=row._mapping["lease_owner"],
        version=row._mapping["version"],
        activated_from_version=row._mapping["activated_from_version"],
        provision_deadline=_aware(row._mapping["provision_deadline"]),  # type: ignore[arg-type]
        heartbeat_at=_aware(row._mapping["heartbeat_at"]),
        expires_at=_aware(row._mapping["lease_expires_at"]),
        released_at=_aware(row._mapping["released_at"]),
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        updated_at=_aware(row._mapping["updated_at"]),  # type: ignore[arg-type]
    )


def _workspace_snapshot(row: Any) -> WorkspaceSnapshotRecord:
    return WorkspaceSnapshotRecord(
        tenant_id=row._mapping["tenant_id"],
        snapshot_id=row._mapping["snapshot_id"],
        run_id=row._mapping["run_id"],
        source_attempt_id=row._mapping["source_attempt_id"],
        execution_unit_id=row._mapping["execution_unit_id"],
        generation=row._mapping["generation"],
        state=WorkspaceSnapshotState(row._mapping["state"]),
        manifest_uri=row._mapping["manifest_uri"],
        checksum=row._mapping["checksum"],
        size_bytes=row._mapping["size_bytes"],
        runtime_image_digest=row._mapping["runtime_image_digest"],
        version=row._mapping["version"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        ready_at=_aware(row._mapping["ready_at"]),
    )


def _artifact(row: Any) -> ArtifactRecord:
    return ArtifactRecord(
        tenant_id=row._mapping["tenant_id"],
        artifact_id=row._mapping["artifact_id"],
        run_id=row._mapping["run_id"],
        logical_name=row._mapping["logical_name"],
        artifact_type=row._mapping["artifact_type"],
        classification=row._mapping["classification"],
        retention_policy=_canonical_json(row._mapping["retention_policy"]),
        state=row._mapping["state"],
        current_version=row._mapping["current_version"],
        version=row._mapping["version"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        updated_at=_aware(row._mapping["updated_at"]),  # type: ignore[arg-type]
    )


def _artifact_version(row: Any) -> ArtifactVersionRecord:
    return ArtifactVersionRecord(
        tenant_id=row._mapping["tenant_id"],
        artifact_id=row._mapping["artifact_id"],
        version=row._mapping["version"],
        run_id=row._mapping["run_id"],
        source_attempt_id=row._mapping["source_attempt_id"],
        generation=row._mapping["generation"],
        state=ArtifactVersionState(row._mapping["state"]),
        state_version=row._mapping["state_version"],
        object_uri=row._mapping["object_uri"],
        checksum=row._mapping["checksum"],
        size_bytes=row._mapping["size_bytes"],
        media_type=row._mapping["media_type"],
        lineage=_canonical_json(row._mapping["lineage"]),
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        ready_at=_aware(row._mapping["ready_at"]),
    )


def _ui_surface(row: Any) -> UiSurfaceRecord:
    return UiSurfaceRecord(
        tenant_id=row._mapping["tenant_id"],
        surface_id=row._mapping["surface_id"],
        run_id=row._mapping["run_id"],
        catalog_id=row._mapping["catalog_id"],
        protocol_version=row._mapping["protocol_version"],
        current_revision=row._mapping["current_revision"],
        status=row._mapping["status"],
        version=row._mapping["version"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        updated_at=_aware(row._mapping["updated_at"]),  # type: ignore[arg-type]
    )


def _ui_surface_revision(row: Any) -> UiSurfaceRevisionRecord:
    return UiSurfaceRevisionRecord(
        tenant_id=row._mapping["tenant_id"],
        surface_id=row._mapping["surface_id"],
        revision=row._mapping["revision"],
        run_id=row._mapping["run_id"],
        source_attempt_id=row._mapping["source_attempt_id"],
        source_generation=row._mapping["source_generation"],
        source_event_seq=row._mapping["source_event_seq"],
        document=_canonical_json(row._mapping["document"]),
        checksum=row._mapping["checksum"],
        validation_result=_canonical_json(row._mapping["validation_result"]),
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
    )


def _action_proposal(row: Any) -> ActionProposalRecord:
    return ActionProposalRecord(
        tenant_id=row._mapping["tenant_id"],
        action_ref=row._mapping["action_ref"],
        run_id=row._mapping["run_id"],
        step_id=row._mapping["step_id"],
        attempt_id=row._mapping["attempt_id"],
        execution_unit_id=row._mapping["execution_unit_id"],
        source_generation=row._mapping["source_generation"],
        tool_name=row._mapping["tool_name"],
        tool_spec_version=row._mapping["tool_spec_version"],
        tool_spec_digest=row._mapping["tool_spec_digest"],
        connector_name=row._mapping["connector_name"],
        required_scopes=tuple(row._mapping["required_scopes"]),
        request_digest=row._mapping["request_digest"],
        canonical_payload_digest=row._mapping["canonical_payload_digest"],
        canonical_target=row._mapping["canonical_target"],
        risk_class=row._mapping["risk_class"],
        status=ActionProposalState(row._mapping["status"]),
        version=row._mapping["version"],
        payload_ref=row._mapping["payload_ref"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        expires_at=_aware(row._mapping["expires_at"]),  # type: ignore[arg-type]
    )


def _approval_request(row: Any) -> ApprovalRequestRecord:
    return ApprovalRequestRecord(
        tenant_id=row._mapping["tenant_id"],
        approval_id=row._mapping["approval_id"],
        run_id=row._mapping["run_id"],
        step_id=row._mapping["step_id"],
        action_ref=row._mapping["action_ref"],
        approval_type=row._mapping["approval_type"],
        request_digest=row._mapping["request_digest"],
        status=ApprovalState(row._mapping["status"]),
        version=row._mapping["version"],
        canonical_request_ref=row._mapping["canonical_request_ref"],
        expires_at=_aware(row._mapping["expires_at"]),  # type: ignore[arg-type]
        decided_by=row._mapping["decided_by"],
        decided_at=_aware(row._mapping["decided_at"]),
        decision_reason=row._mapping["decision_reason"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        updated_at=_aware(row._mapping["updated_at"]),  # type: ignore[arg-type]
    )


def _effect(row: Any) -> EffectLedgerRecord:
    return EffectLedgerRecord(
        tenant_id=row._mapping["tenant_id"],
        effect_id=row._mapping["effect_id"],
        run_id=row._mapping["run_id"],
        action_ref=row._mapping["action_ref"],
        approval_id=row._mapping["approval_id"],
        effect_key=row._mapping["effect_key"],
        request_digest=row._mapping["request_digest"],
        tool_name=row._mapping["tool_name"],
        tool_version=row._mapping["tool_version"],
        tool_spec_digest=row._mapping["tool_spec_digest"],
        connector_name=row._mapping["connector_name"],
        required_scopes=tuple(row._mapping["required_scopes"]),
        canonical_target=row._mapping["canonical_target"],
        canonical_payload_digest=row._mapping["canonical_payload_digest"],
        state=EffectState(row._mapping["state"]),
        version=row._mapping["version"],
        executor_id=row._mapping["executor_id"],
        execution_epoch=row._mapping["execution_epoch"],
        executor_lease_expires_at=_aware(row._mapping["executor_lease_expires_at"]),
        result_ref=row._mapping["result_ref"],
        remote_operation_id=row._mapping["remote_operation_id"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        updated_at=_aware(row._mapping["updated_at"]),  # type: ignore[arg-type]
        completed_at=_aware(row._mapping["completed_at"]),
    )


def _followup_request(row: Any) -> FollowupRequestRecord:
    return FollowupRequestRecord(
        tenant_id=row._mapping["tenant_id"],
        followup_id=row._mapping["followup_id"],
        run_id=row._mapping["run_id"],
        question=row._mapping["question"],
        client_followup_id=row._mapping["client_followup_id"],
        status=row._mapping["status"],
        answer=row._mapping["answer"],
        version=row._mapping["version"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        answered_at=_aware(row._mapping["answered_at"]),
    )


def _inbox_message(row: Any) -> InboxMessageRecord:
    return InboxMessageRecord(
        tenant_id=row._mapping["tenant_id"],
        message_id=row._mapping["message_id"],
        handler_version=row._mapping["handler_version"],
        topic=row._mapping["topic"],
        payload_schema=row._mapping["payload_schema"],
        payload_digest=row._mapping["payload_digest"],
        processing_state=row._mapping["processing_state"],
        version=row._mapping["version"],
        received_at=_aware(row._mapping["received_at"]),  # type: ignore[arg-type]
        processed_at=_aware(row._mapping["processed_at"]),
        failure_code=row._mapping["failure_code"],
    )


def _idempotency(row: Any) -> IdempotencyRecord:
    payload = row._mapping["result_payload"]
    return IdempotencyRecord(
        tenant_id=row._mapping["tenant_id"],
        namespace=row._mapping["command_type"],
        idempotency_key=row._mapping["idempotency_key"],
        request_digest=row._mapping["request_digest"],
        actor_id=row._mapping["actor_id"],
        status=row._mapping["state"],
        result_type=row._mapping["result_type"],
        result_id=row._mapping["result_ref"],
        result_schema=row._mapping["result_schema"],
        result_payload=None if payload is None else _canonical_json(payload),
        version=row._mapping["version"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        updated_at=_aware(row._mapping["updated_at"]),  # type: ignore[arg-type]
        expires_at=_aware(row._mapping["expires_at"])  # type: ignore[arg-type]
        if row._mapping["expires_at"] is not None
        else None,
    )


def _expired(record: IdempotencyRecord, now: datetime) -> bool:
    """True when the record's TTL horizon has passed; ``None`` never expires."""
    return record.expires_at is not None and record.expires_at <= now


def _outbox(row: Any) -> OutboxMessageRecord:
    return OutboxMessageRecord(
        tenant_id=row._mapping["tenant_id"],
        message_id=row._mapping["message_id"],
        run_id=row._mapping["run_id"],
        topic=row._mapping["topic"],
        payload=_canonical_json(row._mapping["payload"]),
        event_id=row._mapping["event_id"],
        aggregate_version=row._mapping["aggregate_version"],
        created_at=_aware(row._mapping["created_at"]),  # type: ignore[arg-type]
        published_at=_aware(row._mapping["published_at"]),
        publish_state=row._mapping["publish_state"],
        version=row._mapping["version"],
        delivery_attempts=row._mapping["delivery_attempts"],
        next_attempt_at=_aware(row._mapping["next_attempt_at"]),
        last_error=row._mapping["last_error"],
        last_error_code=row._mapping["last_error_code"],
    )


def _audit(row: Any) -> AuditEventRecord:
    return AuditEventRecord(
        tenant_id=row._mapping["tenant_id"],
        audit_event_id=row._mapping["audit_id"],
        run_id=row._mapping["run_id"],
        actor_id=row._mapping["actor_id"],
        action=row._mapping["action"],
        entity_type=row._mapping["entity_type"],
        entity_id=row._mapping["entity_id"],
        entity_version=row._mapping["entity_version"],
        outcome=row._mapping["outcome"],
        trace_id=row._mapping["trace_id"],
        details=_canonical_json(row._mapping["detail"]),
        created_at=_aware(row._mapping["occurred_at"]),  # type: ignore[arg-type]
    )


class SqlAlchemyPlatformTransaction:
    def __init__(
        self,
        session: AsyncSession,
        sqlite_l1_clock: Callable[[], datetime] | None,
    ) -> None:
        self._session = session
        self._sqlite_l1_clock = sqlite_l1_clock

    async def db_now(self) -> datetime:
        if self._sqlite_l1_clock is not None:
            value = self._sqlite_l1_clock()
        else:
            value = (await self._session.execute(select(func.current_timestamp()))).scalar_one()
        aware = _aware(value)
        if aware is None:
            raise PlatformError("INTEGRITY_VIOLATION", "database returned no current timestamp")
        return aware

    async def _one(
        self, statement: Select[Any], entity: str, mapper: Callable[[Any], RecordT]
    ) -> RecordT:
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            raise _not_found(entity)
        return mapper(row)

    async def _many(
        self, statement: Select[Any], mapper: Callable[[Any], RecordT]
    ) -> tuple[RecordT, ...]:
        rows = (await self._session.execute(statement)).all()
        return tuple(mapper(row) for row in rows)

    async def lock_run(self, tenant_id: str, run_id: str) -> RunRecord:
        return await self._one(
            select(run_table)
            .where(run_table.c.tenant_id == tenant_id, run_table.c.run_id == run_id)
            .with_for_update(),
            "run",
            _run,
        )

    async def lock_execution_unit(
        self, tenant_id: str, execution_unit_id: str
    ) -> ExecutionUnitRecord:
        return await self._one(
            select(execution_unit_table)
            .where(
                execution_unit_table.c.tenant_id == tenant_id,
                execution_unit_table.c.execution_unit_id == execution_unit_id,
            )
            .with_for_update(),
            "execution unit",
            _unit,
        )

    async def get_run(self, tenant_id: str, run_id: str) -> RunRecord:
        return await self._one(
            select(run_table).where(
                run_table.c.tenant_id == tenant_id, run_table.c.run_id == run_id
            ),
            "run",
            _run,
        )

    async def get_run_authorization_snapshot(
        self, tenant_id: str, run_id: str
    ) -> RunAuthorizationSnapshotRecord:
        return await self._one(
            select(run_authorization_snapshot_table).where(
                run_authorization_snapshot_table.c.tenant_id == tenant_id,
                run_authorization_snapshot_table.c.run_id == run_id,
            ),
            "run authorization snapshot",
            _authorization_snapshot,
        )

    async def get_primary_unit(self, tenant_id: str, run_id: str) -> ExecutionUnitRecord:
        return await self._one(
            select(execution_unit_table).where(
                execution_unit_table.c.tenant_id == tenant_id,
                execution_unit_table.c.run_id == run_id,
                execution_unit_table.c.role == "primary",
            ),
            "primary execution unit",
            _unit,
        )

    async def get_execution_unit(
        self, tenant_id: str, execution_unit_id: str
    ) -> ExecutionUnitRecord:
        return await self._one(
            select(execution_unit_table).where(
                execution_unit_table.c.tenant_id == tenant_id,
                execution_unit_table.c.execution_unit_id == execution_unit_id,
            ),
            "execution unit",
            _unit,
        )

    async def get_checkpoint(self, tenant_id: str, checkpoint_id: str) -> CheckpointRecord:
        return await self._one(
            select(checkpoint_table).where(
                checkpoint_table.c.tenant_id == tenant_id,
                checkpoint_table.c.checkpoint_id == checkpoint_id,
            ),
            "checkpoint",
            _checkpoint,
        )

    async def get_step(self, tenant_id: str, step_id: str) -> StepRecord:
        return await self._one(
            select(step_table).where(
                step_table.c.tenant_id == tenant_id, step_table.c.step_id == step_id
            ),
            "step",
            _step,
        )

    async def get_attempt(self, tenant_id: str, attempt_id: str) -> AttemptRecord:
        return await self._one(
            select(attempt_table).where(
                attempt_table.c.tenant_id == tenant_id,
                attempt_table.c.attempt_id == attempt_id,
            ),
            "attempt",
            _attempt,
        )

    async def get_lease_for_attempt(
        self, tenant_id: str, attempt_id: str
    ) -> ExecutionLeaseRecord:
        return await self._one(
            select(execution_lease_table).where(
                execution_lease_table.c.tenant_id == tenant_id,
                execution_lease_table.c.attempt_id == attempt_id,
            ),
            "execution lease",
            _lease,
        )

    async def get_workspace_snapshot(
        self, tenant_id: str, snapshot_id: str
    ) -> WorkspaceSnapshotRecord:
        return await self._one(
            select(workspace_snapshot_table).where(
                workspace_snapshot_table.c.tenant_id == tenant_id,
                workspace_snapshot_table.c.snapshot_id == snapshot_id,
            ),
            "workspace snapshot",
            _workspace_snapshot,
        )

    async def get_artifact_version(
        self, tenant_id: str, artifact_id: str, version: int
    ) -> ArtifactVersionRecord:
        return await self._one(
            select(artifact_version_table).where(
                artifact_version_table.c.tenant_id == tenant_id,
                artifact_version_table.c.artifact_id == artifact_id,
                artifact_version_table.c.version == version,
            ),
            "artifact version",
            _artifact_version,
        )

    async def get_ui_surface(self, tenant_id: str, surface_id: str) -> UiSurfaceRecord | None:
        row = (
            await self._session.execute(
                select(ui_surface_table).where(
                    ui_surface_table.c.tenant_id == tenant_id,
                    ui_surface_table.c.surface_id == surface_id,
                )
            )
        ).one_or_none()
        return None if row is None else _ui_surface(row)

    async def get_ui_surface_revision(
        self, tenant_id: str, surface_id: str, revision: int
    ) -> UiSurfaceRevisionRecord:
        return await self._one(
            select(ui_surface_revision_table).where(
                ui_surface_revision_table.c.tenant_id == tenant_id,
                ui_surface_revision_table.c.surface_id == surface_id,
                ui_surface_revision_table.c.revision == revision,
            ),
            "UI surface revision",
            _ui_surface_revision,
        )

    async def get_action_proposal(self, tenant_id: str, action_ref: str) -> ActionProposalRecord:
        return await self._one(
            select(action_proposal_table).where(
                action_proposal_table.c.tenant_id == tenant_id,
                action_proposal_table.c.action_ref == action_ref,
            ),
            "action proposal",
            _action_proposal,
        )

    async def get_approval_request(
        self, tenant_id: str, approval_id: str
    ) -> ApprovalRequestRecord:
        return await self._one(
            select(approval_request_table).where(
                approval_request_table.c.tenant_id == tenant_id,
                approval_request_table.c.approval_id == approval_id,
            ),
            "approval request",
            _approval_request,
        )

    async def get_effect(self, tenant_id: str, effect_id: str) -> EffectLedgerRecord:
        return await self._one(
            select(effect_ledger_table).where(
                effect_ledger_table.c.tenant_id == tenant_id,
                effect_ledger_table.c.effect_id == effect_id,
            ),
            "effect",
            _effect,
        )

    async def get_effect_by_key(
        self, tenant_id: str, effect_key: str
    ) -> EffectLedgerRecord | None:
        row = (
            await self._session.execute(
                select(effect_ledger_table).where(
                    effect_ledger_table.c.tenant_id == tenant_id,
                    effect_ledger_table.c.effect_key == effect_key,
                )
            )
        ).one_or_none()
        return None if row is None else _effect(row)

    async def get_outbox_message(self, tenant_id: str, message_id: str) -> OutboxMessageRecord:
        return await self._one(
            select(outbox_message_table).where(
                outbox_message_table.c.tenant_id == tenant_id,
                outbox_message_table.c.message_id == message_id,
            ),
            "outbox message",
            _outbox,
        )

    async def get_inbox_message(
        self, tenant_id: str, message_id: str, handler_version: str
    ) -> InboxMessageRecord:
        return await self._one(
            select(inbox_message_table).where(
                inbox_message_table.c.tenant_id == tenant_id,
                inbox_message_table.c.message_id == message_id,
                inbox_message_table.c.handler_version == handler_version,
            ),
            "inbox message",
            _inbox_message,
        )

    async def list_execution_units_for_run(
        self, tenant_id: str, run_id: str
    ) -> tuple[ExecutionUnitRecord, ...]:
        return await self._many(
            select(execution_unit_table)
            .where(
                execution_unit_table.c.tenant_id == tenant_id,
                execution_unit_table.c.run_id == run_id,
            )
            .order_by(execution_unit_table.c.execution_unit_id),
            _unit,
        )

    async def list_active_attempts_for_run(
        self, tenant_id: str, run_id: str
    ) -> tuple[AttemptRecord, ...]:
        return await self._many(
            select(attempt_table)
            .where(
                attempt_table.c.tenant_id == tenant_id,
                attempt_table.c.run_id == run_id,
                attempt_table.c.status.in_(ACTIVE_ATTEMPT_STATES),
            )
            .order_by(attempt_table.c.attempt_id),
            _attempt,
        )

    async def list_attempts_for_run(
        self, tenant_id: str, run_id: str
    ) -> tuple[AttemptRecord, ...]:
        return await self._many(
            select(attempt_table)
            .where(
                attempt_table.c.tenant_id == tenant_id,
                attempt_table.c.run_id == run_id,
            )
            .order_by(attempt_table.c.generation, attempt_table.c.attempt_id),
            _attempt,
        )

    async def list_active_leases_for_run(
        self, tenant_id: str, run_id: str
    ) -> tuple[ExecutionLeaseRecord, ...]:
        return await self._many(
            select(execution_lease_table)
            .where(
                execution_lease_table.c.tenant_id == tenant_id,
                execution_lease_table.c.run_id == run_id,
                execution_lease_table.c.state.in_(ACTIVE_LEASE_STATES),
            )
            .order_by(execution_lease_table.c.lease_id),
            _lease,
        )

    async def list_events_for_run(
        self, tenant_id: str, run_id: str
    ) -> tuple[EnterpriseEventEnvelope, ...]:
        rows = (
            await self._session.execute(
                select(run_event_table)
                .where(
                    run_event_table.c.tenant_id == tenant_id,
                    run_event_table.c.run_id == run_id,
                )
                .order_by(run_event_table.c.event_seq)
            )
        ).mappings().all()
        return tuple(EnterpriseEventEnvelope.model_validate(dict(row)) for row in rows)

    async def list_ui_surfaces_for_run(
        self, tenant_id: str, run_id: str
    ) -> tuple[UiSurfaceRecord, ...]:
        return await self._many(
            select(ui_surface_table)
            .where(
                ui_surface_table.c.tenant_id == tenant_id,
                ui_surface_table.c.run_id == run_id,
            )
            .order_by(ui_surface_table.c.surface_id),
            _ui_surface,
        )

    async def get_event_retention_floor(self, tenant_id: str, run_id: str) -> int:
        await self.get_run(tenant_id, run_id)
        return 0

    async def list_schedulable_work(self) -> tuple[SchedulableWork, ...]:
        active_attempt = exists(
            select(attempt_table.c.attempt_id).where(
                attempt_table.c.tenant_id == execution_unit_table.c.tenant_id,
                attempt_table.c.run_id == execution_unit_table.c.run_id,
                attempt_table.c.status.in_(ACTIVE_ATTEMPT_STATES),
            )
        )
        active_lease = exists(
            select(execution_lease_table.c.lease_id).where(
                execution_lease_table.c.tenant_id == execution_unit_table.c.tenant_id,
                execution_lease_table.c.run_id == execution_unit_table.c.run_id,
                execution_lease_table.c.state.in_(ACTIVE_LEASE_STATES),
            )
        )
        rows = (
            await self._session.execute(
                select(
                    execution_unit_table.c.tenant_id,
                    execution_unit_table.c.execution_unit_id,
                    execution_unit_table.c.run_id,
                    execution_unit_table.c.current_checkpoint_id,
                )
                .join(
                    run_table,
                    (run_table.c.tenant_id == execution_unit_table.c.tenant_id)
                    & (run_table.c.run_id == execution_unit_table.c.run_id),
                )
                .join(
                    checkpoint_table,
                    (checkpoint_table.c.tenant_id == execution_unit_table.c.tenant_id)
                    & (
                        checkpoint_table.c.checkpoint_id
                        == execution_unit_table.c.current_checkpoint_id
                    ),
                )
                .where(
                    run_table.c.status.in_(
                        (
                            RunState.QUEUED.value,
                            RunState.RUNNING.value,
                            RunState.RECOVERING.value,
                        )
                    ),
                    run_table.c.cancel_requested_at.is_(None),
                    execution_unit_table.c.status.in_(
                        (
                            ExecutionUnitState.DISPATCHABLE.value,
                            ExecutionUnitState.RECOVERING.value,
                        )
                    ),
                    checkpoint_table.c.state == CheckpointState.COMMITTED.value,
                    ~active_attempt,
                    ~active_lease,
                )
                .order_by(
                    execution_unit_table.c.tenant_id,
                    run_table.c.created_at,
                    run_table.c.run_id,
                    execution_unit_table.c.execution_unit_id,
                )
            )
        ).all()
        candidates: list[SchedulableWork] = []
        for row in rows:
            candidates.append(
                SchedulableWork(
                    run=await self.get_run(row._mapping["tenant_id"], row._mapping["run_id"]),
                    unit=await self.get_execution_unit(row._mapping["tenant_id"], row._mapping["execution_unit_id"]),
                    checkpoint=await self.get_checkpoint(
                        row._mapping["tenant_id"], row._mapping["current_checkpoint_id"]
                    ),
                )
            )
        return tuple(candidates)

    async def claim_idempotency(
        self,
        tenant_id: str,
        namespace: str,
        idempotency_key: str,
        request_digest: str,
        actor_id: str,
        now: datetime,
    ) -> IdempotencyRecord | None:
        values = {
            "tenant_id": tenant_id,
            "command_type": namespace,
            "idempotency_key": idempotency_key,
            "actor_id": actor_id,
            "request_digest": request_digest,
            "state": "IN_PROGRESS",
            "result_type": None,
            "result_schema": None,
            "result_ref": None,
            "result_payload": None,
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + IDEMPOTENCY_RETENTION,
        }
        dialect = self._session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = postgresql_insert(idempotency_record_table)
        elif dialect == "sqlite":
            statement = sqlite_insert(idempotency_record_table)
        else:
            raise RuntimeError(f"unsupported SQLAlchemy dialect: {dialect}")
        inserted = (
            await self._session.execute(
                statement.values(**values)
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "command_type", "idempotency_key"]
                )
                .returning(idempotency_record_table.c.command_type)
            )
        ).scalar_one_or_none()
        row = (
            await self._session.execute(
                select(idempotency_record_table).where(
                    idempotency_record_table.c.tenant_id == tenant_id,
                    idempotency_record_table.c.command_type == namespace,
                    idempotency_record_table.c.idempotency_key == idempotency_key,
                )
            )
        ).one()
        record = _idempotency(row)
        if inserted is not None:
            return None
        # TTL expired (SDD §13.2): recycle the key as a fresh claim so abandoned
        # IN_PROGRESS claims and old COMPLETED keys never block reuse.
        if _expired(record, now):
            recycled = {
                "actor_id": actor_id,
                "request_digest": request_digest,
                "state": "IN_PROGRESS",
                "result_type": None,
                "result_schema": None,
                "result_ref": None,
                "result_payload": None,
                "version": idempotency_record_table.c.version + 1,
                "created_at": now,
                "updated_at": now,
                "expires_at": now + IDEMPOTENCY_RETENTION,
            }
            refreshed = (
                await self._session.execute(
                    update(idempotency_record_table)
                    .where(
                        idempotency_record_table.c.tenant_id == tenant_id,
                        idempotency_record_table.c.command_type == namespace,
                        idempotency_record_table.c.idempotency_key == idempotency_key,
                    )
                    .values(**recycled)
                    .returning(*idempotency_record_table.c)
                )
            ).one_or_none()
            if refreshed is not None:
                return None
            # Another writer refreshed first; re-read and apply normal rules.
            row = (
                await self._session.execute(
                    select(idempotency_record_table).where(
                        idempotency_record_table.c.tenant_id == tenant_id,
                        idempotency_record_table.c.command_type == namespace,
                        idempotency_record_table.c.idempotency_key == idempotency_key,
                    )
                )
            ).one()
            record = _idempotency(row)
        if record.request_digest != request_digest or record.actor_id != actor_id:
            raise PlatformError(
                "IDEMPOTENCY_KEY_REUSED",
                "idempotency key was already used for another request",
            )
        return record

    async def complete_idempotency(
        self,
        tenant_id: str,
        namespace: str,
        idempotency_key: str,
        request_digest: str,
        result_type: str,
        result_id: str,
        result_schema: str,
        result_payload: dict[str, object],
        now: datetime,
    ) -> IdempotencyRecord:
        if IDEMPOTENCY_RESULT_SCHEMAS.get(result_type) != result_schema:
            raise PlatformError(
                "INTEGRITY_VIOLATION", "idempotency result type and schema do not match"
            )
        statement = (
            update(idempotency_record_table)
            .where(
                idempotency_record_table.c.tenant_id == tenant_id,
                idempotency_record_table.c.command_type == namespace,
                idempotency_record_table.c.idempotency_key == idempotency_key,
                idempotency_record_table.c.request_digest == request_digest,
                idempotency_record_table.c.state == "IN_PROGRESS",
            )
            .values(
                state="COMPLETED",
                result_type=result_type,
                result_ref=result_id,
                result_schema=result_schema,
                result_payload=_canonical_json(result_payload),
                version=idempotency_record_table.c.version + 1,
                updated_at=now,
                expires_at=now + IDEMPOTENCY_RETENTION,
            )
            .returning(*idempotency_record_table.c)
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            raise PlatformError("INTEGRITY_VIOLATION", "idempotency claim is missing or complete")
        return _idempotency(row)

    async def purge_expired_idempotency(self, limit: int) -> int:
        now = await self.db_now()
        expired = (
            await self._session.execute(
                select(
                    idempotency_record_table.c.tenant_id,
                    idempotency_record_table.c.command_type,
                    idempotency_record_table.c.idempotency_key,
                )
                .where(idempotency_record_table.c.expires_at.is_not(None))
                .where(idempotency_record_table.c.expires_at <= now)
                .order_by(idempotency_record_table.c.created_at)
                .limit(limit)
            )
        ).all()
        if not expired:
            return 0
        # Portable delete by primary-key tuples (DELETE ... LIMIT is not valid
        # on PostgreSQL; row-value IN works on both PG and SQLite).
        await self._session.execute(
            delete(idempotency_record_table).where(
                tuple_(
                    idempotency_record_table.c.tenant_id,
                    idempotency_record_table.c.command_type,
                    idempotency_record_table.c.idempotency_key,
                ).in_(expired)
            )
        )
        return len(expired)

    async def _insert(self, table: TableClause, values: dict[str, object], entity: str) -> None:
        try:
            await self._session.execute(insert(table).values(**values))
            await self._session.flush()
        except IntegrityError as error:
            raise PlatformError("INTEGRITY_VIOLATION", f"invalid {entity} relation") from error

    async def insert_run(self, record: RunRecord) -> None:
        values = _record_values(record)
        values["status"] = record.status.value
        values["resource_refs"] = list(record.resource_refs)
        values["parameters"] = _canonical_json(record.parameters)
        await self._insert(run_table, values, "run")

    async def insert_run_authorization_snapshot(
        self, record: RunAuthorizationSnapshotRecord
    ) -> None:
        await self._insert(
            run_authorization_snapshot_table,
            {
                "tenant_id": record.tenant_id,
                "run_id": record.run_id,
                "resolved_resources": [
                    _canonical_json(resource) for resource in record.resolved_resources
                ],
                "host_context_digest": record.host_context_digest,
                "host_context_version": record.host_context_version,
                "policy_digest": record.policy_digest,
                "policy_version": record.policy_version,
                "policy_scopes": list(record.policy_scopes),
                "policy_budget": _canonical_json(record.policy_budget),
                "snapshot_digest": record.snapshot_digest,
                "created_at": record.created_at,
            },
            "run authorization snapshot",
        )

    async def insert_execution_unit(self, record: ExecutionUnitRecord) -> None:
        values = _record_values(record)
        values["status"] = record.status.value
        await self._insert(execution_unit_table, values, "execution unit")

    async def insert_checkpoint(self, record: CheckpointRecord) -> None:
        values = _record_values(record)
        values["state"] = record.state.value
        values["workflow_cursor"] = _canonical_json(record.workflow_cursor)
        values["completed_step_ids"] = list(record.completed_step_ids)
        values["active_step_context"] = _canonical_json(record.active_step_context)
        values["input_artifact_versions"] = [
            _canonical_json(item) for item in record.input_artifact_versions
        ]
        values["output_artifact_versions"] = [
            _canonical_json(item) for item in record.output_artifact_versions
        ]
        values["resolved_tool_call_ids"] = list(record.resolved_tool_call_ids)
        values["effect_states"] = _canonical_json(record.effect_states)
        values["budget_consumed"] = _canonical_json(record.budget_consumed)
        values["agent_state"] = _canonical_json(record.agent_state)
        await self._insert(checkpoint_table, values, "checkpoint")

    async def insert_step(self, record: StepRecord) -> None:
        await self._insert(
            step_table,
            {
                "tenant_id": record.tenant_id,
                "step_id": record.step_id,
                "run_id": record.run_id,
                "ordinal": record.ordinal,
                "name": record.name,
                "step_type": record.step_type,
                "policy_snapshot": _canonical_json(record.policy_snapshot),
                "status": record.status.value,
                "status_reason": record.status_reason,
                "version": record.version,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "ended_at": record.ended_at,
            },
            "step",
        )

    async def insert_attempt(self, record: AttemptRecord) -> None:
        values = _record_values(record)
        values["status"] = record.status.value
        await self._insert(attempt_table, values, "attempt")

    async def insert_lease(self, record: ExecutionLeaseRecord) -> None:
        values = _record_values(record)
        values["state"] = record.state.value
        values["lease_owner"] = values.pop("owner")
        values["lease_expires_at"] = values.pop("expires_at")
        await self._insert(execution_lease_table, values, "execution lease")

    async def insert_workspace_snapshot(self, record: WorkspaceSnapshotRecord) -> None:
        await self._insert(
            workspace_snapshot_table,
            {
                "tenant_id": record.tenant_id,
                "snapshot_id": record.snapshot_id,
                "run_id": record.run_id,
                "source_attempt_id": record.source_attempt_id,
                "execution_unit_id": record.execution_unit_id,
                "generation": record.generation,
                "state": record.state.value,
                "manifest_uri": record.manifest_uri,
                "checksum": record.checksum,
                "size_bytes": record.size_bytes,
                "runtime_image_digest": record.runtime_image_digest,
                "version": record.version,
                "created_at": record.created_at,
                "ready_at": record.ready_at,
            },
            "workspace snapshot",
        )

    async def insert_artifact(self, record: ArtifactRecord) -> None:
        await self._insert(
            artifact_table,
            {
                "tenant_id": record.tenant_id,
                "artifact_id": record.artifact_id,
                "run_id": record.run_id,
                "logical_name": record.logical_name,
                "artifact_type": record.artifact_type,
                "classification": record.classification,
                "retention_policy": _canonical_json(record.retention_policy),
                "state": record.state,
                "current_version": record.current_version,
                "version": record.version,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            },
            "artifact",
        )

    async def insert_artifact_version(self, record: ArtifactVersionRecord) -> None:
        await self._insert(
            artifact_version_table,
            {
                "tenant_id": record.tenant_id,
                "artifact_id": record.artifact_id,
                "version": record.version,
                "run_id": record.run_id,
                "source_attempt_id": record.source_attempt_id,
                "generation": record.generation,
                "state": record.state.value,
                "state_version": record.state_version,
                "object_uri": record.object_uri,
                "checksum": record.checksum,
                "size_bytes": record.size_bytes,
                "media_type": record.media_type,
                "lineage": _canonical_json(record.lineage),
                "created_at": record.created_at,
                "ready_at": record.ready_at,
            },
            "artifact version",
        )

    async def insert_ui_surface(self, record: UiSurfaceRecord) -> None:
        await self._insert(
            ui_surface_table,
            {
                "tenant_id": record.tenant_id,
                "surface_id": record.surface_id,
                "run_id": record.run_id,
                "catalog_id": record.catalog_id,
                "protocol_version": record.protocol_version,
                "current_revision": record.current_revision,
                "status": record.status,
                "version": record.version,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            },
            "UI surface",
        )

    async def insert_ui_surface_revision(self, record: UiSurfaceRevisionRecord) -> None:
        await self._insert(
            ui_surface_revision_table,
            {
                "tenant_id": record.tenant_id,
                "surface_id": record.surface_id,
                "revision": record.revision,
                "run_id": record.run_id,
                "source_attempt_id": record.source_attempt_id,
                "source_generation": record.source_generation,
                "source_event_seq": record.source_event_seq,
                "document": _canonical_json(record.document),
                "checksum": record.checksum,
                "validation_result": _canonical_json(record.validation_result),
                "created_at": record.created_at,
            },
            "UI surface revision",
        )

    async def insert_action_proposal(self, record: ActionProposalRecord) -> None:
        validate_new_action_proposal(record)
        values = _record_values(record)
        values["status"] = record.status.value
        values["required_scopes"] = list(record.required_scopes)
        await self._insert(action_proposal_table, values, "action proposal")

    async def insert_approval_request(self, record: ApprovalRequestRecord) -> None:
        values = _record_values(record)
        values["status"] = record.status.value
        await self._insert(approval_request_table, values, "approval request")

    async def insert_effect(self, record: EffectLedgerRecord) -> None:
        proposal = await self.get_action_proposal(record.tenant_id, record.action_ref)
        approval = await self.get_approval_request(record.tenant_id, record.approval_id)
        validate_new_effect(record, proposal=proposal, approval=approval)
        values = _record_values(record)
        values["state"] = record.state.value
        values["required_scopes"] = list(record.required_scopes)
        await self._insert(effect_ledger_table, values, "effect")

    async def claim_inbox_message(self, record: InboxMessageRecord) -> bool:
        if record.processing_state != "RECEIVED" or record.version != 1:
            raise PlatformError(
                "INTEGRITY_VIOLATION", "new Inbox message must be RECEIVED at version 1"
            )
        values = _record_values(record)
        dialect = self._session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = postgresql_insert(inbox_message_table)
        elif dialect == "sqlite":
            statement = sqlite_insert(inbox_message_table)
        else:
            raise RuntimeError(f"unsupported SQLAlchemy dialect: {dialect}")
        inserted = (
            await self._session.execute(
                statement.values(**values)
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "message_id", "handler_version"]
                )
                .returning(inbox_message_table.c.message_id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            return True
        existing = await self.get_inbox_message(
            record.tenant_id, record.message_id, record.handler_version
        )
        if (
            existing.topic != record.topic
            or existing.payload_schema != record.payload_schema
            or existing.payload_digest != record.payload_digest
        ):
            raise PlatformError(
                "INBOX_MESSAGE_REUSED",
                "message identity was already used for another payload",
            )
        return False

    async def _replace_cas(
        self,
        table: TableClause,
        identity_column: Any,
        identity: str,
        tenant_id: str,
        values: dict[str, object],
        expected_version: int,
        entity: str,
    ) -> None:
        if values["version"] != expected_version + 1:
            raise PlatformError("VERSION_CONFLICT", f"{entity} version compare-and-swap failed")
        row = (
            await self._session.execute(
                update(table)
                .where(
                    table.c.tenant_id == tenant_id,
                    identity_column == identity,
                    table.c.version == expected_version,
                )
                .values(**values)
                .returning(identity_column)
            )
        ).scalar_one_or_none()
        if row is not None:
            return
        exists_row = (
            await self._session.execute(
                select(identity_column).where(
                    table.c.tenant_id == tenant_id, identity_column == identity
                )
            )
        ).scalar_one_or_none()
        if exists_row is None:
            raise _not_found(entity)
        raise PlatformError("VERSION_CONFLICT", f"{entity} version compare-and-swap failed")

    async def replace_run_cas(self, record: RunRecord, expected_version: int) -> None:
        values = _record_values(record)
        values.pop("tenant_id")
        values.pop("run_id")
        values["status"] = record.status.value
        values["resource_refs"] = list(record.resource_refs)
        values["parameters"] = _canonical_json(record.parameters)
        await self._replace_cas(
            run_table,
            run_table.c.run_id,
            record.run_id,
            record.tenant_id,
            values,
            expected_version,
            "run",
        )

    async def replace_execution_unit_cas(
        self, record: ExecutionUnitRecord, expected_version: int
    ) -> None:
        values = _record_values(record)
        values.pop("tenant_id")
        values.pop("execution_unit_id")
        values["status"] = record.status.value
        await self._replace_cas(
            execution_unit_table,
            execution_unit_table.c.execution_unit_id,
            record.execution_unit_id,
            record.tenant_id,
            values,
            expected_version,
            "execution unit",
        )

    async def replace_checkpoint_cas(
        self, record: CheckpointRecord, expected_version: int
    ) -> None:
        values = _record_values(record)
        values.pop("tenant_id")
        values.pop("checkpoint_id")
        values["state"] = record.state.value
        values["workflow_cursor"] = _canonical_json(record.workflow_cursor)
        values["completed_step_ids"] = list(record.completed_step_ids)
        values["active_step_context"] = _canonical_json(record.active_step_context)
        values["input_artifact_versions"] = [
            _canonical_json(item) for item in record.input_artifact_versions
        ]
        values["output_artifact_versions"] = [
            _canonical_json(item) for item in record.output_artifact_versions
        ]
        values["resolved_tool_call_ids"] = list(record.resolved_tool_call_ids)
        values["effect_states"] = _canonical_json(record.effect_states)
        values["budget_consumed"] = _canonical_json(record.budget_consumed)
        values["agent_state"] = _canonical_json(record.agent_state)
        await self._replace_cas(
            checkpoint_table,
            checkpoint_table.c.checkpoint_id,
            record.checkpoint_id,
            record.tenant_id,
            values,
            expected_version,
            "checkpoint",
        )

    async def replace_step_cas(self, record: StepRecord, expected_version: int) -> None:
        values = _record_values(record)
        values.pop("tenant_id")
        values.pop("step_id")
        values["status"] = record.status.value
        values["policy_snapshot"] = _canonical_json(record.policy_snapshot)
        await self._replace_cas(
            step_table,
            step_table.c.step_id,
            record.step_id,
            record.tenant_id,
            values,
            expected_version,
            "step",
        )

    async def replace_attempt_cas(self, record: AttemptRecord, expected_version: int) -> None:
        values = _record_values(record)
        values.pop("tenant_id")
        values.pop("attempt_id")
        values["status"] = record.status.value
        await self._replace_cas(
            attempt_table,
            attempt_table.c.attempt_id,
            record.attempt_id,
            record.tenant_id,
            values,
            expected_version,
            "attempt",
        )

    async def replace_lease_cas(self, record: ExecutionLeaseRecord, expected_version: int) -> None:
        values = _record_values(record)
        values.pop("tenant_id")
        values.pop("lease_id")
        values["state"] = record.state.value
        values["lease_owner"] = values.pop("owner")
        values["lease_expires_at"] = values.pop("expires_at")
        await self._replace_cas(
            execution_lease_table,
            execution_lease_table.c.lease_id,
            record.lease_id,
            record.tenant_id,
            values,
            expected_version,
            "execution lease",
        )

    async def replace_inbox_message_cas(
        self, record: InboxMessageRecord, expected_version: int
    ) -> None:
        if record.version != expected_version + 1:
            raise PlatformError("VERSION_CONFLICT", "inbox message version compare-and-swap failed")
        values = _record_values(record)
        values.pop("tenant_id")
        values.pop("message_id")
        values.pop("handler_version")
        row = (
            await self._session.execute(
                update(inbox_message_table)
                .where(
                    inbox_message_table.c.tenant_id == record.tenant_id,
                    inbox_message_table.c.message_id == record.message_id,
                    inbox_message_table.c.handler_version == record.handler_version,
                    inbox_message_table.c.version == expected_version,
                )
                .values(**values)
                .returning(inbox_message_table.c.message_id)
            )
        ).scalar_one_or_none()
        if row is not None:
            return
        existing = (
            await self._session.execute(
                select(inbox_message_table.c.message_id).where(
                    inbox_message_table.c.tenant_id == record.tenant_id,
                    inbox_message_table.c.message_id == record.message_id,
                    inbox_message_table.c.handler_version == record.handler_version,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise _not_found("inbox message")
        raise PlatformError("VERSION_CONFLICT", "inbox message version compare-and-swap failed")

    async def replace_action_proposal_cas(
        self, record: ActionProposalRecord, expected_version: int
    ) -> None:
        current = await self.get_action_proposal(record.tenant_id, record.action_ref)
        if replace(record, status=current.status, version=current.version) != current:
            raise PlatformError("INTEGRITY_VIOLATION", "action proposal facts are immutable")
        values = _record_values(record)
        values.pop("tenant_id")
        values.pop("action_ref")
        values["status"] = record.status.value
        values["required_scopes"] = list(record.required_scopes)
        await self._replace_cas(
            action_proposal_table,
            action_proposal_table.c.action_ref,
            record.action_ref,
            record.tenant_id,
            values,
            expected_version,
            "action proposal",
        )

    async def replace_approval_request_cas(
        self, record: ApprovalRequestRecord, expected_version: int
    ) -> None:
        current = await self.get_approval_request(record.tenant_id, record.approval_id)
        immutable_candidate = replace(
            record,
            status=current.status,
            version=current.version,
            decided_by=current.decided_by,
            decided_at=current.decided_at,
            decision_reason=current.decision_reason,
            updated_at=current.updated_at,
        )
        if immutable_candidate != current:
            raise PlatformError("INTEGRITY_VIOLATION", "approval request facts are immutable")
        values = _record_values(record)
        values.pop("tenant_id")
        values.pop("approval_id")
        values["status"] = record.status.value
        await self._replace_cas(
            approval_request_table,
            approval_request_table.c.approval_id,
            record.approval_id,
            record.tenant_id,
            values,
            expected_version,
            "approval request",
        )

    async def insert_followup_request(self, record: FollowupRequestRecord) -> None:
        if record.status not in ("PENDING", "ANSWERED") or record.version != 1:
            raise PlatformError(
                "INTEGRITY_VIOLATION",
                "followup request must be PENDING/ANSWERED at version 1 on insert",
            )
        await self._insert(followup_request_table, _record_values(record), "followup request")

    async def get_followup_request(
        self, tenant_id: str, followup_id: str
    ) -> FollowupRequestRecord:
        statement = (
            select(followup_request_table)
            .where(followup_request_table.c.tenant_id == tenant_id)
            .where(followup_request_table.c.followup_id == followup_id)
        )
        result = await self._session.execute(statement)
        row = result.first()
        if row is None:
            raise _not_found("followup request")
        return _followup_request(row)

    async def list_followup_requests(
        self, tenant_id: str, run_id: str
    ) -> tuple[FollowupRequestRecord, ...]:
        statement = (
            select(followup_request_table)
            .where(followup_request_table.c.tenant_id == tenant_id)
            .where(followup_request_table.c.run_id == run_id)
            .order_by(followup_request_table.c.created_at)
        )
        rows = (await self._session.execute(statement)).all()
        return tuple(_followup_request(row) for row in rows)

    async def replace_followup_request_cas(
        self, record: FollowupRequestRecord, expected_version: int
    ) -> None:
        current = await self.get_followup_request(record.tenant_id, record.followup_id)
        if current.version != expected_version or record.version != expected_version + 1:
            raise PlatformError("VERSION_CONFLICT", "followup request version compare-and-swap failed")
        if current.status != "PENDING" or record.status != "ANSWERED":
            raise PlatformError("INTEGRITY_VIOLATION", "followup request may only PENDING -> ANSWERED")
        values = _record_values(record)
        values.pop("tenant_id")
        values.pop("followup_id")
        await self._replace_cas(
            followup_request_table,
            followup_request_table.c.followup_id,
            record.followup_id,
            record.tenant_id,
            values,
            expected_version,
            "followup request",
        )

    async def replace_effect_cas(self, record: EffectLedgerRecord, expected_version: int) -> None:
        current = await self.get_effect(record.tenant_id, record.effect_id)
        if current.version != expected_version or record.version != expected_version + 1:
            raise PlatformError("VERSION_CONFLICT", "effect version compare-and-swap failed")
        validate_effect_update(current, record)
        immutable_candidate = replace(
            record,
            state=current.state,
            version=current.version,
            executor_id=current.executor_id,
            execution_epoch=current.execution_epoch,
            executor_lease_expires_at=current.executor_lease_expires_at,
            result_ref=current.result_ref,
            remote_operation_id=current.remote_operation_id,
            updated_at=current.updated_at,
            completed_at=current.completed_at,
        )
        if immutable_candidate != current:
            raise PlatformError("INTEGRITY_VIOLATION", "effect identity is immutable")
        values = _record_values(record)
        values.pop("tenant_id")
        values.pop("effect_id")
        values["state"] = record.state.value
        values["required_scopes"] = list(record.required_scopes)
        await self._replace_cas(
            effect_ledger_table,
            effect_ledger_table.c.effect_id,
            record.effect_id,
            record.tenant_id,
            values,
            expected_version,
            "effect",
        )

    async def replace_outbox_cas(self, record: OutboxMessageRecord, expected_version: int) -> None:
        current = await self.get_outbox_message(record.tenant_id, record.message_id)
        immutable_candidate = replace(
            record,
            publish_state=current.publish_state,
            version=current.version,
            delivery_attempts=current.delivery_attempts,
            next_attempt_at=current.next_attempt_at,
            last_error=current.last_error,
            last_error_code=current.last_error_code,
            published_at=current.published_at,
        )
        if immutable_candidate != current:
            raise PlatformError("INTEGRITY_VIOLATION", "outbox message facts are immutable")
        values = {
            "publish_state": record.publish_state,
            "version": record.version,
            "delivery_attempts": record.delivery_attempts,
            "next_attempt_at": record.next_attempt_at,
            "last_error": record.last_error,
            "last_error_code": record.last_error_code,
            "published_at": record.published_at,
        }
        await self._replace_cas(
            outbox_message_table,
            outbox_message_table.c.message_id,
            record.message_id,
            record.tenant_id,
            values,
            expected_version,
            "outbox message",
        )

    async def replace_ui_surface_cas(self, record: UiSurfaceRecord, expected_version: int) -> None:
        values = _record_values(record)
        values.pop("tenant_id")
        values.pop("surface_id")
        await self._replace_cas(
            ui_surface_table,
            ui_surface_table.c.surface_id,
            record.surface_id,
            record.tenant_id,
            values,
            expected_version,
            "UI surface",
        )

    async def append_event(
        self, event: EnterpriseEventEnvelope, expected_previous_seq: int
    ) -> None:
        latest = (
            await self._session.execute(
                select(run_event_table.c.event_seq)
                .where(
                    run_event_table.c.tenant_id == event.tenant_id,
                    run_event_table.c.run_id == event.run_id,
                )
                .order_by(run_event_table.c.event_seq.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        actual = 0 if latest is None else latest
        if actual != expected_previous_seq or event.event_seq != expected_previous_seq + 1:
            raise PlatformError("INTEGRITY_VIOLATION", "event sequence or relation is invalid")
        values = event.model_dump(mode="python")
        values["event_type"] = event.event_type.value
        await self._insert(run_event_table, values, "event")

    async def insert_outbox(self, record: OutboxMessageRecord) -> None:
        validate_new_outbox(record)
        await self._insert(
            outbox_message_table,
            {
                "tenant_id": record.tenant_id,
                "message_id": record.message_id,
                "run_id": record.run_id,
                "aggregate_type": "run",
                "aggregate_id": record.run_id,
                "aggregate_version": record.aggregate_version,
                "topic": record.topic,
                "schema_version": "outbox-message/v1",
                "payload_schema": "outbox-message/v1",
                "payload": _canonical_json(record.payload),
                "publish_state": record.publish_state,
                "version": record.version,
                "event_id": record.event_id,
                "created_at": record.created_at,
                "published_at": record.published_at,
                "delivery_attempts": record.delivery_attempts,
                "next_attempt_at": record.next_attempt_at,
                "last_error": record.last_error,
                "last_error_code": record.last_error_code,
            },
            "outbox message",
        )

    async def insert_audit(self, record: AuditEventRecord) -> None:
        await self._insert(
            audit_event_table,
            {
                "tenant_id": record.tenant_id,
                "audit_id": record.audit_event_id,
                "run_id": record.run_id,
                "actor_id": record.actor_id,
                "initiating_actor": record.actor_id,
                "action": record.action,
                "entity_type": record.entity_type,
                "entity_id": record.entity_id,
                "entity_version": record.entity_version,
                "outcome": record.outcome,
                "policy_version": None,
                "grant_id": None,
                "approval_id": None,
                "effect_id": None,
                "trace_id": record.trace_id,
                "detail": _canonical_json(record.details),
                "occurred_at": record.created_at,
            },
            "audit event",
        )


class SqlAlchemyPlatformStore:
    """Async store with one session and one explicit transaction per unit of work."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        id_factory: Callable[[str], str] | None = None,
        sqlite_l1_clock: Callable[[], datetime] | None = None,
    ) -> None:
        bind = session_factory.kw.get("bind")
        if sqlite_l1_clock is not None and getattr(bind, "dialect", None).name != "sqlite":
            raise ValueError("sqlite_l1_clock may only be used with SQLite L1 tests")
        self._session_factory = session_factory
        self._id_factory = id_factory or (lambda kind: f"{kind}_{uuid4().hex}")
        self._sqlite_l1_clock = sqlite_l1_clock

    def new_id(self, kind: str) -> str:
        return self._id_factory(kind)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[PlatformTransaction]:
        async with self._session_factory() as session, session.begin():
            if session.get_bind().dialect.name == "sqlite":
                # L1 mirrors PostgreSQL's deferred circular cursor FK.
                await session.execute(text("PRAGMA defer_foreign_keys=ON"))
            yield SqlAlchemyPlatformTransaction(session, self._sqlite_l1_clock)

    async def _read(self, operation: Callable[[SqlAlchemyPlatformTransaction], Any]) -> Any:
        async with self._session_factory() as session, session.begin():
            transaction = SqlAlchemyPlatformTransaction(session, self._sqlite_l1_clock)
            return await operation(transaction)

    async def get_run(self, tenant_id: str, run_id: str) -> RunRecord:
        return await self._read(lambda tx: tx.get_run(tenant_id, run_id))

    async def get_run_authorization_snapshot(
        self, tenant_id: str, run_id: str
    ) -> RunAuthorizationSnapshotRecord:
        return await self._read(lambda tx: tx.get_run_authorization_snapshot(tenant_id, run_id))

    async def get_primary_unit(self, tenant_id: str, run_id: str) -> ExecutionUnitRecord:
        return await self._read(lambda tx: tx.get_primary_unit(tenant_id, run_id))

    async def get_execution_unit(
        self, tenant_id: str, execution_unit_id: str
    ) -> ExecutionUnitRecord:
        return await self._read(lambda tx: tx.get_execution_unit(tenant_id, execution_unit_id))

    async def get_checkpoint(self, tenant_id: str, checkpoint_id: str) -> CheckpointRecord:
        return await self._read(lambda tx: tx.get_checkpoint(tenant_id, checkpoint_id))

    async def get_step(self, tenant_id: str, step_id: str) -> StepRecord:
        return await self._read(lambda tx: tx.get_step(tenant_id, step_id))

    async def get_attempt(self, tenant_id: str, attempt_id: str) -> AttemptRecord:
        return await self._read(lambda tx: tx.get_attempt(tenant_id, attempt_id))

    async def get_lease_for_attempt(
        self, tenant_id: str, attempt_id: str
    ) -> ExecutionLeaseRecord:
        return await self._read(lambda tx: tx.get_lease_for_attempt(tenant_id, attempt_id))

    async def get_workspace_snapshot(
        self, tenant_id: str, snapshot_id: str
    ) -> WorkspaceSnapshotRecord:
        return await self._read(lambda tx: tx.get_workspace_snapshot(tenant_id, snapshot_id))

    async def get_artifact_version(
        self, tenant_id: str, artifact_id: str, version: int
    ) -> ArtifactVersionRecord:
        return await self._read(lambda tx: tx.get_artifact_version(tenant_id, artifact_id, version))

    async def get_ui_surface(self, tenant_id: str, surface_id: str) -> UiSurfaceRecord | None:
        return await self._read(lambda tx: tx.get_ui_surface(tenant_id, surface_id))

    async def get_ui_surface_revision(
        self, tenant_id: str, surface_id: str, revision: int
    ) -> UiSurfaceRevisionRecord:
        return await self._read(
            lambda tx: tx.get_ui_surface_revision(tenant_id, surface_id, revision)
        )

    async def get_action_proposal(self, tenant_id: str, action_ref: str) -> ActionProposalRecord:
        return await self._read(lambda tx: tx.get_action_proposal(tenant_id, action_ref))

    async def get_approval_request(
        self, tenant_id: str, approval_id: str
    ) -> ApprovalRequestRecord:
        return await self._read(lambda tx: tx.get_approval_request(tenant_id, approval_id))

    async def get_effect(self, tenant_id: str, effect_id: str) -> EffectLedgerRecord:
        return await self._read(lambda tx: tx.get_effect(tenant_id, effect_id))

    async def get_followup_request(
        self, tenant_id: str, followup_id: str
    ) -> FollowupRequestRecord:
        return await self._read(lambda tx: tx.get_followup_request(tenant_id, followup_id))

    async def list_followup_requests(
        self, tenant_id: str, run_id: str
    ) -> tuple[FollowupRequestRecord, ...]:
        return await self._read(lambda tx: tx.list_followup_requests(tenant_id, run_id))

    async def get_effect_by_key(
        self, tenant_id: str, effect_key: str
    ) -> EffectLedgerRecord | None:
        return await self._read(lambda tx: tx.get_effect_by_key(tenant_id, effect_key))

    async def get_outbox_message(self, tenant_id: str, message_id: str) -> OutboxMessageRecord:
        return await self._read(lambda tx: tx.get_outbox_message(tenant_id, message_id))

    async def get_inbox_message(
        self, tenant_id: str, message_id: str, handler_version: str
    ) -> InboxMessageRecord:
        return await self._read(
            lambda tx: tx.get_inbox_message(tenant_id, message_id, handler_version)
        )

    async def list_schedulable_work(self) -> tuple[SchedulableWork, ...]:
        return await self._read(lambda tx: tx.list_schedulable_work())

    async def list_events(
        self, tenant_id: str, run_id: str
    ) -> tuple[EnterpriseEventEnvelope, ...]:
        return await self._read(lambda tx: tx.list_events_for_run(tenant_id, run_id))

    async def list_ui_surfaces(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[UiSurfaceRecord, ...]:
        async def read(tx: SqlAlchemyPlatformTransaction) -> tuple[UiSurfaceRecord, ...]:
            statement = select(ui_surface_table).where(ui_surface_table.c.tenant_id == tenant_id)
            if run_id is not None:
                statement = statement.where(ui_surface_table.c.run_id == run_id)
            return await tx._many(statement.order_by(ui_surface_table.c.created_at), _ui_surface)

        return await self._read(read)

    async def list_outbox(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[OutboxMessageRecord, ...]:
        async def read(tx: SqlAlchemyPlatformTransaction) -> tuple[OutboxMessageRecord, ...]:
            statement = select(outbox_message_table).where(
                outbox_message_table.c.tenant_id == tenant_id
            )
            if run_id is not None:
                statement = statement.where(outbox_message_table.c.run_id == run_id)
            return await tx._many(statement.order_by(outbox_message_table.c.created_at), _outbox)

        return await self._read(read)

    async def list_pending_outbox(self, *, limit: int = 100) -> tuple[OutboxMessageRecord, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("outbox limit must be between 1 and 1000")

        async def read(tx: SqlAlchemyPlatformTransaction) -> tuple[OutboxMessageRecord, ...]:
            now = await tx.db_now()
            statement = (
                select(outbox_message_table)
                .where(
                    outbox_message_table.c.publish_state == "PENDING",
                    (outbox_message_table.c.next_attempt_at.is_(None))
                    | (outbox_message_table.c.next_attempt_at <= now),
                )
                .order_by(
                    outbox_message_table.c.created_at,
                    outbox_message_table.c.tenant_id,
                    outbox_message_table.c.message_id,
                )
                .limit(limit)
            )
            return await tx._many(statement, _outbox)

        return await self._read(read)

    async def list_audit_events(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[AuditEventRecord, ...]:
        async def read(tx: SqlAlchemyPlatformTransaction) -> tuple[AuditEventRecord, ...]:
            statement = select(audit_event_table).where(audit_event_table.c.tenant_id == tenant_id)
            if run_id is not None:
                statement = statement.where(audit_event_table.c.run_id == run_id)
            return await tx._many(
                statement.order_by(audit_event_table.c.occurred_at), _audit
            )

        return await self._read(read)

    async def list_approval_requests(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[ApprovalRequestRecord, ...]:
        async def read(tx: SqlAlchemyPlatformTransaction) -> tuple[ApprovalRequestRecord, ...]:
            statement = select(approval_request_table).where(
                approval_request_table.c.tenant_id == tenant_id
            )
            if run_id is not None:
                statement = statement.where(approval_request_table.c.run_id == run_id)
            return await tx._many(
                statement.order_by(approval_request_table.c.created_at), _approval_request
            )

        return await self._read(read)

    async def list_effects(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[EffectLedgerRecord, ...]:
        async def read(tx: SqlAlchemyPlatformTransaction) -> tuple[EffectLedgerRecord, ...]:
            statement = select(effect_ledger_table).where(
                effect_ledger_table.c.tenant_id == tenant_id
            )
            if run_id is not None:
                statement = statement.where(effect_ledger_table.c.run_id == run_id)
            return await tx._many(statement.order_by(effect_ledger_table.c.created_at), _effect)

        return await self._read(read)
