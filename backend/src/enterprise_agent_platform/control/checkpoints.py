"""Generation-fenced Checkpoint commits and atomic approval pauses."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import cast

from pydantic import JsonValue

from enterprise_agent_platform.contracts.enums import (
    ActionProposalState,
    ApprovalState,
    ArtifactVersionState,
    AttemptState,
    CheckpointState,
    EntityType,
    EventType,
    ExecutionLeaseState,
    ExecutionUnitState,
    RunState,
    StepState,
    WorkspaceSnapshotState,
)
from enterprise_agent_platform.contracts.events import (
    AttemptLifecyclePayload,
    EnterpriseEventEnvelope,
    RunStatusChangedPayload,
)
from enterprise_agent_platform.domain.fsm import transition
from enterprise_agent_platform.domain.records import (
    ApprovalRequestRecord,
    AttemptRecord,
    AuditEventRecord,
    CheckpointRecord,
    ExecutionLeaseRecord,
    ExecutionUnitRecord,
    OutboxMessageRecord,
    RunRecord,
)
from enterprise_agent_platform.persistence.protocol import (
    PlatformError,
    PlatformStore,
    PlatformTransaction,
)

from .context import RequestContext


@dataclass(frozen=True, slots=True)
class ArtifactVersionRef:
    artifact_id: str
    version: int

    def __post_init__(self) -> None:
        if not self.artifact_id or self.version < 1:
            raise ValueError("artifact_id and version are required")


@dataclass(frozen=True, slots=True)
class CheckpointCommit:
    """Metadata whose referenced immutable bytes were prepared before this command."""
    source_checkpoint_id: str
    workflow_cursor: dict[str, JsonValue]
    checksum: str
    completed_step_ids: tuple[str, ...] = ()
    active_step_context: dict[str, JsonValue] = field(default_factory=dict)
    input_artifact_versions: tuple[ArtifactVersionRef, ...] = ()
    output_artifact_versions: tuple[ArtifactVersionRef, ...] = ()
    workspace_snapshot_id: str | None = None
    resolved_tool_call_ids: tuple[str, ...] = ()
    effect_states: dict[str, JsonValue] = field(default_factory=dict)
    budget_consumed: dict[str, JsonValue] = field(default_factory=dict)
    model_context_summary_ref: str | None = None
    runtime_image_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.source_checkpoint_id or not self.checksum:
            raise ValueError("source_checkpoint_id and checksum are required")
        object.__setattr__(self, "completed_step_ids", tuple(self.completed_step_ids))
        object.__setattr__(self, "input_artifact_versions", tuple(self.input_artifact_versions))
        object.__setattr__(self, "output_artifact_versions", tuple(self.output_artifact_versions))
        object.__setattr__(self, "resolved_tool_call_ids", tuple(self.resolved_tool_call_ids))


@dataclass(frozen=True, slots=True)
class ApprovalPause:
    step_id: str
    action_ref: str
    approval_type: str
    request_digest: str
    canonical_request_ref: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not all(
            (
                self.step_id,
                self.action_ref,
                self.approval_type,
                self.request_digest,
                self.canonical_request_ref,
            )
        ):
            raise ValueError("approval pause identifiers are required")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("approval expiry must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ApprovalPauseResult:
    checkpoint: CheckpointRecord
    approval: ApprovalRequestRecord


@dataclass(frozen=True, slots=True)
class _RuntimeFacts:
    run: RunRecord
    unit: ExecutionUnitRecord
    attempt: AttemptRecord
    lease: ExecutionLeaseRecord
    source: CheckpointRecord


def _canonical_object(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    try:
        canonical = json.loads(
            json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
        )
    except (TypeError, ValueError) as error:
        raise PlatformError(
            "INTEGRITY_VIOLATION", "Checkpoint JSON metadata is not canonicalizable"
        ) from error
    if not isinstance(canonical, dict):
        raise PlatformError("INTEGRITY_VIOLATION", "Checkpoint metadata must be an object")
    return cast(dict[str, JsonValue], canonical)


def _prepared(command: CheckpointCommit) -> CheckpointCommit:
    """Freeze caller-owned containers before the transactional metadata commit."""
    return replace(
        command,
        workflow_cursor=_canonical_object(command.workflow_cursor),
        active_step_context=_canonical_object(command.active_step_context),
        effect_states=_canonical_object(command.effect_states),
        budget_consumed=_canonical_object(command.budget_consumed),
    )


async def _runtime_facts(
    tx: PlatformTransaction,
    tenant_id: str,
    attempt_id: str,
    generation: int,
    lease_owner: str,
    expected_lease_version: int,
    source_checkpoint_id: str,
    now: datetime,
) -> _RuntimeFacts:
    candidate_attempt = await tx.get_attempt(tenant_id, attempt_id)
    run = await tx.lock_run(tenant_id, candidate_attempt.run_id)
    unit = await tx.lock_execution_unit(tenant_id, candidate_attempt.execution_unit_id)
    attempt = await tx.get_attempt(tenant_id, attempt_id)
    lease = await tx.get_lease_for_attempt(tenant_id, attempt_id)
    try:
        source = await tx.get_checkpoint(tenant_id, source_checkpoint_id)
    except PlatformError as error:
        if error.code != "NOT_FOUND":
            raise
        raise PlatformError("SOURCE_CHECKPOINT_INVALID", "source Checkpoint does not exist") from error
    if (
        attempt.run_id != run.run_id
        or attempt.execution_unit_id != unit.execution_unit_id
        or unit.run_id != run.run_id
        or lease.run_id != run.run_id
        or lease.execution_unit_id != unit.execution_unit_id
        or lease.attempt_id != attempt.attempt_id
    ):
        raise PlatformError("INTEGRITY_VIOLATION", "runtime facts are inconsistent")
    if (
        generation != attempt.generation
        or generation != lease.generation
        or generation != unit.next_generation
    ):
        raise PlatformError("STALE_GENERATION", "runtime generation fence rejected the write")
    if lease.state is not ExecutionLeaseState.ACTIVE:
        raise PlatformError("LEASE_NOT_ACTIVE", "runtime Lease is not active")
    if lease.owner != lease_owner:
        raise PlatformError("LEASE_OWNER_MISMATCH", "runtime does not own the Lease")
    if lease.version != expected_lease_version:
        raise PlatformError("VERSION_CONFLICT", "Lease version compare-and-swap failed")
    if lease.expires_at is None or now >= lease.expires_at:
        raise PlatformError("LEASE_EXPIRED", "runtime Lease has expired")
    if attempt.status is not AttemptState.CHECKPOINTING:
        raise PlatformError("INVALID_STATE", "Attempt is not checkpointing")
    if unit.status is not ExecutionUnitState.EXECUTING or run.status is not RunState.RUNNING:
        raise PlatformError("INVALID_STATE", "Run execution is not active")
    if (
        source.checkpoint_id != unit.current_checkpoint_id
        or source.run_id != run.run_id
        or source.execution_unit_id != unit.execution_unit_id
        or source.state is not CheckpointState.COMMITTED
    ):
        raise PlatformError(
            "SOURCE_CHECKPOINT_INVALID",
            "source Checkpoint must be the unit's current committed cursor",
        )
    return _RuntimeFacts(run=run, unit=unit, attempt=attempt, lease=lease, source=source)


async def _ready_references(
    tx: PlatformTransaction,
    facts: _RuntimeFacts,
    command: CheckpointCommit,
) -> tuple[
    tuple[dict[str, JsonValue], ...],
    tuple[dict[str, JsonValue], ...],
    str | None,
]:
    snapshot_runtime_digest: str | None = None
    if command.workspace_snapshot_id is not None:
        try:
            snapshot = await tx.get_workspace_snapshot(
                facts.run.tenant_id, command.workspace_snapshot_id
            )
        except PlatformError as error:
            if error.code != "NOT_FOUND":
                raise
            raise PlatformError("SNAPSHOT_NOT_READY", "Workspace Snapshot is not READY") from error
        if (
            snapshot.state is not WorkspaceSnapshotState.READY
            or snapshot.ready_at is None
            or snapshot.run_id != facts.run.run_id
            or snapshot.execution_unit_id != facts.unit.execution_unit_id
            or snapshot.source_attempt_id != facts.attempt.attempt_id
            or snapshot.generation != facts.attempt.generation
        ):
            raise PlatformError("SNAPSHOT_NOT_READY", "Workspace Snapshot is not READY")
        snapshot_runtime_digest = snapshot.runtime_image_digest

    async def load_artifacts(
        references: tuple[ArtifactVersionRef, ...], require_current_attempt: bool
    ) -> tuple[dict[str, JsonValue], ...]:
        loaded: list[dict[str, JsonValue]] = []
        seen: set[tuple[str, int]] = set()
        for reference in references:
            key = (reference.artifact_id, reference.version)
            if key in seen:
                raise PlatformError(
                    "INTEGRITY_VIOLATION", "Checkpoint contains a duplicate Artifact version"
                )
            seen.add(key)
            try:
                version = await tx.get_artifact_version(
                    facts.run.tenant_id, reference.artifact_id, reference.version
                )
            except PlatformError as error:
                if error.code != "NOT_FOUND":
                    raise
                raise PlatformError("ARTIFACT_NOT_READY", "Artifact version is not READY") from error
            if (
                version.state is not ArtifactVersionState.READY
                or version.ready_at is None
                or version.run_id != facts.run.run_id
                or (
                    require_current_attempt
                    and (
                        version.source_attempt_id != facts.attempt.attempt_id
                        or version.generation != facts.attempt.generation
                    )
                )
            ):
                raise PlatformError("ARTIFACT_NOT_READY", "Artifact version is not READY")
            loaded.append(
                cast(
                    dict[str, JsonValue],
                    {"artifact_id": version.artifact_id, "version": version.version},
                )
            )
        return tuple(loaded)

    input_versions = await load_artifacts(
        command.input_artifact_versions, require_current_attempt=False
    )
    output_versions = await load_artifacts(
        command.output_artifact_versions, require_current_attempt=True
    )
    if (
        command.runtime_image_digest is not None
        and snapshot_runtime_digest is not None
        and command.runtime_image_digest != snapshot_runtime_digest
    ):
        raise PlatformError(
            "SNAPSHOT_RUNTIME_MISMATCH",
            "Workspace Snapshot runtime image does not match the Checkpoint",
        )
    return (
        input_versions,
        output_versions,
        (command.runtime_image_digest or snapshot_runtime_digest),
    )


def _checkpoint(
    store: PlatformStore,
    facts: _RuntimeFacts,
    command: CheckpointCommit,
    event_seq: int,
    now: datetime,
    input_versions: tuple[dict[str, JsonValue], ...],
    output_versions: tuple[dict[str, JsonValue], ...],
    runtime_image_digest: str | None,
) -> CheckpointRecord:
    return CheckpointRecord(
        tenant_id=facts.run.tenant_id,
        checkpoint_id=store.new_id("checkpoint"),
        run_id=facts.run.run_id,
        execution_unit_id=facts.unit.execution_unit_id,
        source_attempt_id=facts.attempt.attempt_id,
        checkpoint_seq=facts.source.checkpoint_seq + 1,
        state=CheckpointState.COMMITTED,
        workflow_cursor=command.workflow_cursor,
        last_event_seq=event_seq,
        workspace_snapshot_id=command.workspace_snapshot_id,
        checkpoint_schema_version=facts.source.checkpoint_schema_version,
        runtime_profile_version=facts.source.runtime_profile_version,
        policy_version=facts.source.policy_version,
        tool_catalog_version=facts.source.tool_catalog_version,
        ui_catalog_version=facts.source.ui_catalog_version,
        checksum=command.checksum,
        version=1,
        created_at=now,
        committed_at=now,
        completed_step_ids=command.completed_step_ids,
        active_step_context=command.active_step_context,
        input_artifact_versions=input_versions,
        output_artifact_versions=output_versions,
        resolved_tool_call_ids=command.resolved_tool_call_ids,
        effect_states=command.effect_states,
        budget_consumed=command.budget_consumed,
        model_context_summary_ref=command.model_context_summary_ref,
        runtime_image_digest=runtime_image_digest,
    )


def _attempt_event(
    store: PlatformStore,
    ctx: RequestContext,
    facts: _RuntimeFacts,
    status: AttemptState,
    event_seq: int,
    now: datetime,
) -> EnterpriseEventEnvelope:
    return EnterpriseEventEnvelope(
        schema_version="enterprise-event/v1",
        event_id=store.new_id("event"),
        tenant_id=ctx.tenant_id,
        run_id=facts.run.run_id,
        event_seq=event_seq,
        event_type=EventType.ATTEMPT_LIFECYCLE,
        occurred_at=now,
        producer_service="control-plane",
        payload_schema="attempt-lifecycle/v1",
        payload=AttemptLifecyclePayload(
            kind="attempt.lifecycle",
            attempt_id=facts.attempt.attempt_id,
            status=status,
        ),
        attempt_id=facts.attempt.attempt_id,
        trace_id=ctx.trace_id,
    )


async def commit_checkpoint(
    store: PlatformStore,
    ctx: RequestContext,
    *,
    attempt_id: str,
    generation: int,
    lease_owner: str,
    expected_lease_version: int,
    command: CheckpointCommit,
) -> CheckpointRecord:
    command = _prepared(command)
    async with store.transaction() as tx:
        now = await tx.db_now()
        facts = await _runtime_facts(
            tx,
            tenant_id=ctx.tenant_id,
            attempt_id=attempt_id,
            generation=generation,
            lease_owner=lease_owner,
            expected_lease_version=expected_lease_version,
            source_checkpoint_id=command.source_checkpoint_id,
            now=now,
        )
        input_versions, output_versions, runtime_image_digest = await _ready_references(
            tx, facts, command
        )
        transition(
            EntityType.ATTEMPT,
            facts.attempt.status,
            AttemptState.RUNNING,
            command,
        )
        event = _attempt_event(
            store,
            ctx,
            facts,
            AttemptState.RUNNING,
            event_seq=facts.run.last_event_seq + 1,
            now=now,
        )
        checkpoint = _checkpoint(
            store,
            facts,
            command,
            event_seq=event.event_seq,
            now=now,
            input_versions=input_versions,
            output_versions=output_versions,
            runtime_image_digest=runtime_image_digest,
        )
        resumed_attempt = replace(
            facts.attempt,
            status=AttemptState.RUNNING,
            version=facts.attempt.version + 1,
            updated_at=now,
        )
        advanced_unit = replace(
            facts.unit,
            current_checkpoint_id=checkpoint.checkpoint_id,
            version=facts.unit.version + 1,
            updated_at=now,
        )
        advanced_run = replace(
            facts.run,
            version=facts.run.version + 1,
            last_event_seq=event.event_seq,
            updated_at=now,
        )
        audit = AuditEventRecord(
            tenant_id=ctx.tenant_id,
            audit_event_id=store.new_id("audit"),
            run_id=facts.run.run_id,
            actor_id=ctx.actor_id,
            action="checkpoint.committed",
            entity_type="checkpoint",
            entity_id=checkpoint.checkpoint_id,
            entity_version=checkpoint.version,
            outcome="COMMITTED",
            trace_id=ctx.trace_id,
            details={
                "attempt_id": facts.attempt.attempt_id,
                "generation": facts.attempt.generation,
                "checkpoint_seq": checkpoint.checkpoint_seq,
                "checksum": checkpoint.checksum,
            },
            created_at=now,
        )
        outbox = OutboxMessageRecord(
            tenant_id=ctx.tenant_id,
            message_id=store.new_id("outbox"),
            run_id=facts.run.run_id,
            topic="checkpoint.committed",
            payload={
                "checkpoint_id": checkpoint.checkpoint_id,
                "attempt_id": facts.attempt.attempt_id,
                "generation": facts.attempt.generation,
            },
            event_id=event.event_id,
            aggregate_version=advanced_run.version,
            created_at=now,
            published_at=None,
        )
        await tx.insert_checkpoint(checkpoint)
        await tx.replace_attempt_cas(resumed_attempt, facts.attempt.version)
        await tx.replace_execution_unit_cas(advanced_unit, facts.unit.version)
        await tx.replace_run_cas(advanced_run, facts.run.version)
        await tx.append_event(event, facts.run.last_event_seq)
        await tx.insert_audit(audit)
        await tx.insert_outbox(outbox)
        return checkpoint


async def pause_for_approval(
    store: PlatformStore,
    ctx: RequestContext,
    *,
    attempt_id: str,
    generation: int,
    lease_owner: str,
    expected_lease_version: int,
    checkpoint: CheckpointCommit,
    approval: ApprovalPause,
) -> ApprovalPauseResult:
    checkpoint = _prepared(checkpoint)
    async with store.transaction() as tx:
        now = await tx.db_now()
        facts = await _runtime_facts(
            tx,
            tenant_id=ctx.tenant_id,
            attempt_id=attempt_id,
            generation=generation,
            lease_owner=lease_owner,
            expected_lease_version=expected_lease_version,
            source_checkpoint_id=checkpoint.source_checkpoint_id,
            now=now,
        )
        step = await tx.get_step(ctx.tenant_id, approval.step_id)
        proposal = await tx.get_action_proposal(ctx.tenant_id, approval.action_ref)
        if (
            step.run_id != facts.run.run_id
            or step.status is not StepState.ACTIVE
            or facts.attempt.step_id != step.step_id
        ):
            raise PlatformError("INVALID_STATE", "Step is not active for this Attempt")
        if (
            proposal.run_id != facts.run.run_id
            or proposal.step_id != step.step_id
            or proposal.attempt_id != facts.attempt.attempt_id
            or proposal.execution_unit_id != facts.unit.execution_unit_id
            or proposal.source_generation != facts.attempt.generation
            or proposal.request_digest != approval.request_digest
            or proposal.payload_ref != approval.canonical_request_ref
            or proposal.status is not ActionProposalState.OPEN
        ):
            raise PlatformError("APPROVAL_DIGEST_MISMATCH", "Action proposal does not match")
        if now >= proposal.expires_at or now >= approval.expires_at:
            raise PlatformError("APPROVAL_EXPIRED", "Approval request has expired")
        if approval.expires_at > proposal.expires_at:
            raise PlatformError(
                "APPROVAL_EXPIRY_INVALID",
                "Approval cannot outlive its canonical Action proposal",
            )
        input_versions, output_versions, runtime_image_digest = await _ready_references(
            tx, facts, checkpoint
        )
        transition(
            EntityType.ATTEMPT,
            facts.attempt.status,
            AttemptState.CHECKPOINTED_FOR_APPROVAL,
            approval,
        )
        transition(EntityType.STEP, step.status, StepState.WAITING_APPROVAL, approval)
        transition(
            EntityType.EXECUTION_UNIT,
            facts.unit.status,
            ExecutionUnitState.WAITING_APPROVAL,
            approval,
        )
        transition(EntityType.RUN, facts.run.status, RunState.WAITING_APPROVAL, approval)
        transition(
            EntityType.EXECUTION_LEASE,
            facts.lease.state,
            ExecutionLeaseState.RELEASED,
            approval,
        )
        attempt_event = _attempt_event(
            store,
            ctx,
            facts,
            AttemptState.CHECKPOINTED_FOR_APPROVAL,
            event_seq=facts.run.last_event_seq + 1,
            now=now,
        )
        run_event = EnterpriseEventEnvelope(
            schema_version="enterprise-event/v1",
            event_id=store.new_id("event"),
            tenant_id=ctx.tenant_id,
            run_id=facts.run.run_id,
            event_seq=facts.run.last_event_seq + 2,
            event_type=EventType.RUN_STATUS_CHANGED,
            occurred_at=now,
            producer_service="control-plane",
            payload_schema="run-status/v1",
            payload=RunStatusChangedPayload(
                kind="run.status.changed",
                previous=facts.run.status,
                current=RunState.WAITING_APPROVAL,
            ),
            attempt_id=facts.attempt.attempt_id,
            causation_event_id=attempt_event.event_id,
            trace_id=ctx.trace_id,
        )
        committed = _checkpoint(
            store,
            facts,
            checkpoint,
            event_seq=run_event.event_seq,
            now=now,
            input_versions=input_versions,
            output_versions=output_versions,
            runtime_image_digest=runtime_image_digest,
        )
        approval_record = ApprovalRequestRecord(
            tenant_id=ctx.tenant_id,
            approval_id=store.new_id("approval"),
            run_id=facts.run.run_id,
            step_id=step.step_id,
            action_ref=proposal.action_ref,
            approval_type=approval.approval_type,
            request_digest=approval.request_digest,
            status=ApprovalState.PENDING,
            version=1,
            canonical_request_ref=proposal.payload_ref,
            expires_at=approval.expires_at,
            decided_by=None,
            decided_at=None,
            decision_reason=None,
            created_at=now,
            updated_at=now,
        )
        waiting_step = replace(
            step,
            status=StepState.WAITING_APPROVAL,
            status_reason="APPROVAL_REQUIRED",
            version=step.version + 1,
            updated_at=now,
        )
        paused_attempt = replace(
            facts.attempt,
            status=AttemptState.CHECKPOINTED_FOR_APPROVAL,
            version=facts.attempt.version + 1,
            updated_at=now,
            ended_at=now,
        )
        released_lease = replace(
            facts.lease,
            state=ExecutionLeaseState.RELEASED,
            version=facts.lease.version + 1,
            released_at=now,
            updated_at=now,
        )
        waiting_unit = replace(
            facts.unit,
            status=ExecutionUnitState.WAITING_APPROVAL,
            current_checkpoint_id=committed.checkpoint_id,
            version=facts.unit.version + 1,
            updated_at=now,
        )
        waiting_run = replace(
            facts.run,
            status=RunState.WAITING_APPROVAL,
            status_reason="APPROVAL_REQUIRED",
            version=facts.run.version + 1,
            last_event_seq=run_event.event_seq,
            updated_at=now,
        )
        audit = AuditEventRecord(
            tenant_id=ctx.tenant_id,
            audit_event_id=store.new_id("audit"),
            run_id=facts.run.run_id,
            actor_id=ctx.actor_id,
            action="approval.requested",
            entity_type="approval",
            entity_id=approval_record.approval_id,
            entity_version=approval_record.version,
            outcome="PENDING",
            trace_id=ctx.trace_id,
            details={
                "action_ref": approval_record.action_ref,
                "request_digest": approval_record.request_digest,
                "checkpoint_id": committed.checkpoint_id,
                "generation": facts.attempt.generation,
            },
            created_at=now,
        )
        outbox = (
            OutboxMessageRecord(
                tenant_id=ctx.tenant_id,
                message_id=store.new_id("outbox"),
                run_id=facts.run.run_id,
                topic="runtime.capability.revoke.requested",
                payload={
                    "attempt_id": facts.attempt.attempt_id,
                    "generation": facts.attempt.generation,
                },
                event_id=run_event.event_id,
                aggregate_version=waiting_run.version,
                created_at=now,
                published_at=None,
            ),
            OutboxMessageRecord(
                tenant_id=ctx.tenant_id,
                message_id=store.new_id("outbox"),
                run_id=facts.run.run_id,
                topic="sandbox.delete.requested",
                payload={
                    "attempt_id": facts.attempt.attempt_id,
                    "generation": facts.attempt.generation,
                },
                event_id=run_event.event_id,
                aggregate_version=waiting_run.version,
                created_at=now,
                published_at=None,
            ),
        )
        await tx.insert_checkpoint(committed)
        await tx.insert_approval_request(approval_record)
        await tx.replace_step_cas(waiting_step, step.version)
        await tx.replace_attempt_cas(paused_attempt, facts.attempt.version)
        await tx.replace_lease_cas(released_lease, facts.lease.version)
        await tx.replace_execution_unit_cas(waiting_unit, facts.unit.version)
        await tx.replace_run_cas(waiting_run, facts.run.version)
        await tx.append_event(attempt_event, facts.run.last_event_seq)
        await tx.append_event(run_event, attempt_event.event_seq)
        await tx.insert_audit(audit)
        for message in outbox:
            await tx.insert_outbox(message)
        return ApprovalPauseResult(checkpoint=committed, approval=approval_record)


__all__ = [
    "ApprovalPause",
    "ApprovalPauseResult",
    "ArtifactVersionRef",
    "CheckpointCommit",
    "commit_checkpoint",
    "pause_for_approval",
]
