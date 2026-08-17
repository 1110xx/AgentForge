"""Serializable copy-on-write in-memory implementation of the persistence ports."""
from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import uuid4

from enterprise_agent_platform.contracts.enums import (
    AttemptState,
    CheckpointState,
    ExecutionLeaseState,
    ExecutionUnitState,
    RunState,
)
from enterprise_agent_platform.contracts.events import EnterpriseEventEnvelope
from enterprise_agent_platform.domain.records import (
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

RunKey = tuple[str, str]
AuthorizationSnapshotKey = tuple[str, str]
UnitKey = tuple[str, str]
CheckpointKey = tuple[str, str]
StepKey = tuple[str, str]
AttemptKey = tuple[str, str]
LeaseKey = tuple[str, str]
SnapshotKey = tuple[str, str]
ArtifactKey = tuple[str, str]
ArtifactVersionKey = tuple[str, str, int]
UiSurfaceKey = tuple[str, str]
UiSurfaceRevisionKey = tuple[str, str, int]
ActionProposalKey = tuple[str, str]
ApprovalKey = tuple[str, str]
InboxKey = tuple[str, str, str]
IdempotencyKey = tuple[str, str, str]
EffectKey = tuple[str, str]

ACTIVE_ATTEMPT_STATES = frozenset({
    AttemptState.CREATED,
    AttemptState.PROVISIONING,
    AttemptState.CLAIMED,
    AttemptState.RUNNING,
    AttemptState.CHECKPOINTING,
})

ACTIVE_LEASE_STATES = frozenset({ExecutionLeaseState.RESERVED, ExecutionLeaseState.ACTIVE})

IDEMPOTENCY_RESULT_SCHEMAS = {
    "approval_decision": "approval-decision/v1",
    "effect_recovery": "effect-recovery/v1",
    "run": "run-record/v1",
    "attempt_reservation": "attempt-reservation/v1",
}


@dataclass(slots=True)
class _State:
    runs: dict[RunKey, RunRecord] = field(default_factory=dict)
    authorization_snapshots: dict[AuthorizationSnapshotKey, RunAuthorizationSnapshotRecord] = field(
        default_factory=dict
    )
    units: dict[UnitKey, ExecutionUnitRecord] = field(default_factory=dict)
    primary_units: dict[RunKey, UnitKey] = field(default_factory=dict)
    checkpoints: dict[CheckpointKey, CheckpointRecord] = field(default_factory=dict)
    steps: dict[StepKey, StepRecord] = field(default_factory=dict)
    attempts: dict[AttemptKey, AttemptRecord] = field(default_factory=dict)
    leases: dict[LeaseKey, ExecutionLeaseRecord] = field(default_factory=dict)
    lease_by_attempt: dict[AttemptKey, LeaseKey] = field(default_factory=dict)
    workspace_snapshots: dict[SnapshotKey, WorkspaceSnapshotRecord] = field(default_factory=dict)
    artifacts: dict[ArtifactKey, ArtifactRecord] = field(default_factory=dict)
    artifact_versions: dict[ArtifactVersionKey, ArtifactVersionRecord] = field(default_factory=dict)
    ui_surfaces: dict[UiSurfaceKey, UiSurfaceRecord] = field(default_factory=dict)
    ui_surface_revisions: dict[UiSurfaceRevisionKey, UiSurfaceRevisionRecord] = field(
        default_factory=dict
    )
    action_proposals: dict[ActionProposalKey, ActionProposalRecord] = field(default_factory=dict)
    approvals: dict[ApprovalKey, ApprovalRequestRecord] = field(default_factory=dict)
    effects: dict[EffectKey, EffectLedgerRecord] = field(default_factory=dict)
    effect_by_key: dict[tuple[str, str], EffectKey] = field(default_factory=dict)
    inbox: dict[InboxKey, InboxMessageRecord] = field(default_factory=dict)
    idempotency: dict[IdempotencyKey, IdempotencyRecord] = field(default_factory=dict)
    events: dict[RunKey, list[EnterpriseEventEnvelope]] = field(default_factory=dict)
    outbox: dict[tuple[str, str], OutboxMessageRecord] = field(default_factory=dict)
    audit: dict[tuple[str, str], AuditEventRecord] = field(default_factory=dict)


def _canonical_json_object(value: dict[str, object]) -> dict[str, object]:
    try:
        canonical = json.loads(
            json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    except (TypeError, ValueError) as error:
        raise PlatformError(
            "INTEGRITY_VIOLATION", "persistence JSON value is not canonicalizable"
        ) from error
    if not isinstance(canonical, dict):
        raise PlatformError("INTEGRITY_VIOLATION", "persistence JSON payload must be an object")
    return canonical


def _detached[T](value: T) -> T:
    cloned = copy.deepcopy(value)
    if isinstance(cloned, RunRecord):
        return replace(cloned, parameters=_canonical_json_object(cloned.parameters))
    if isinstance(cloned, RunAuthorizationSnapshotRecord):
        return replace(
            cloned,
            resolved_resources=tuple(
                _canonical_json_object(resource) for resource in cloned.resolved_resources
            ),
            policy_budget=_canonical_json_object(cloned.policy_budget),
        )
    if isinstance(cloned, CheckpointRecord):
        return replace(
            cloned,
            workflow_cursor=_canonical_json_object(cloned.workflow_cursor),
            active_step_context=_canonical_json_object(cloned.active_step_context),
            input_artifact_versions=tuple(
                _canonical_json_object(item) for item in cloned.input_artifact_versions
            ),
            output_artifact_versions=tuple(
                _canonical_json_object(item) for item in cloned.output_artifact_versions
            ),
            effect_states=_canonical_json_object(cloned.effect_states),
            budget_consumed=_canonical_json_object(cloned.budget_consumed),
        )
    if isinstance(cloned, StepRecord):
        return replace(cloned, policy_snapshot=_canonical_json_object(cloned.policy_snapshot))
    if isinstance(cloned, ArtifactRecord):
        return replace(cloned, retention_policy=_canonical_json_object(cloned.retention_policy))
    if isinstance(cloned, ArtifactVersionRecord):
        return replace(cloned, lineage=_canonical_json_object(cloned.lineage))
    if isinstance(cloned, UiSurfaceRevisionRecord):
        return replace(
            cloned,
            document=_canonical_json_object(cloned.document),
            validation_result=_canonical_json_object(cloned.validation_result),
        )
    if isinstance(cloned, OutboxMessageRecord):
        return replace(cloned, payload=_canonical_json_object(cloned.payload))
    if isinstance(cloned, AuditEventRecord):
        return replace(cloned, details=_canonical_json_object(cloned.details))
    if isinstance(cloned, IdempotencyRecord) and cloned.result_payload is not None:
        return replace(
            cloned,
            result_payload=_canonical_json_object(cloned.result_payload),
        )
    return cloned


def _not_found(entity: str) -> PlatformError:
    return PlatformError("NOT_FOUND", f"{entity} was not found")


class _MemoryTransaction:
    def __init__(
        self,
        state: _State,
        clock: Callable[[], datetime],
        fault_injector: Callable[[str], None] | None,
        retention_floor: Callable[[str, str], int],
    ) -> None:
        self._state = state
        self._clock = clock
        self._fault_injector = fault_injector
        self._retention_floor = retention_floor
        self._locked_run: RunKey | None = None

    def _fault(self, operation: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(operation)

    async def db_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise PlatformError("INTEGRITY_VIOLATION", "database clock must be timezone-aware")
        return now

    async def lock_run(self, tenant_id: str, run_id: str) -> RunRecord:
        record = await self.get_run(tenant_id, run_id)
        self._locked_run = (tenant_id, run_id)
        return record

    async def lock_execution_unit(
        self, tenant_id: str, execution_unit_id: str
    ) -> ExecutionUnitRecord:
        record = await self.get_execution_unit(tenant_id, execution_unit_id)
        if self._locked_run != (tenant_id, record.run_id):
            raise PlatformError(
                "INTEGRITY_VIOLATION", "execution units must be locked after their run"
            )
        return record

    async def get_run(self, tenant_id: str, run_id: str) -> RunRecord:
        try:
            return _detached(self._state.runs[(tenant_id, run_id)])
        except KeyError as error:
            raise _not_found("run") from error

    async def get_run_authorization_snapshot(
        self, tenant_id: str, run_id: str
    ) -> RunAuthorizationSnapshotRecord:
        try:
            return _detached(self._state.authorization_snapshots[(tenant_id, run_id)])
        except KeyError as error:
            raise _not_found("run authorization snapshot") from error

    async def get_primary_unit(self, tenant_id: str, run_id: str) -> ExecutionUnitRecord:
        try:
            unit_key = self._state.primary_units[(tenant_id, run_id)]
            return _detached(self._state.units[unit_key])
        except KeyError as error:
            raise _not_found("primary execution unit") from error

    async def get_execution_unit(
        self, tenant_id: str, execution_unit_id: str
    ) -> ExecutionUnitRecord:
        try:
            return _detached(self._state.units[(tenant_id, execution_unit_id)])
        except KeyError as error:
            raise _not_found("execution unit") from error

    async def get_checkpoint(self, tenant_id: str, checkpoint_id: str) -> CheckpointRecord:
        try:
            return _detached(self._state.checkpoints[(tenant_id, checkpoint_id)])
        except KeyError as error:
            raise _not_found("checkpoint") from error

    async def get_step(self, tenant_id: str, step_id: str) -> StepRecord:
        try:
            return _detached(self._state.steps[(tenant_id, step_id)])
        except KeyError as error:
            raise _not_found("step") from error

    async def get_attempt(self, tenant_id: str, attempt_id: str) -> AttemptRecord:
        try:
            return _detached(self._state.attempts[(tenant_id, attempt_id)])
        except KeyError as error:
            raise _not_found("attempt") from error

    async def get_lease_for_attempt(
        self, tenant_id: str, attempt_id: str
    ) -> ExecutionLeaseRecord:
        try:
            lease_key = self._state.lease_by_attempt[(tenant_id, attempt_id)]
            return _detached(self._state.leases[lease_key])
        except KeyError as error:
            raise _not_found("execution lease") from error

    async def get_workspace_snapshot(
        self, tenant_id: str, snapshot_id: str
    ) -> WorkspaceSnapshotRecord:
        try:
            return _detached(self._state.workspace_snapshots[(tenant_id, snapshot_id)])
        except KeyError as error:
            raise _not_found("workspace snapshot") from error

    async def get_artifact_version(
        self, tenant_id: str, artifact_id: str, version: int
    ) -> ArtifactVersionRecord:
        try:
            return _detached(self._state.artifact_versions[(tenant_id, artifact_id, version)])
        except KeyError as error:
            raise _not_found("artifact version") from error

    async def get_ui_surface(self, tenant_id: str, surface_id: str) -> UiSurfaceRecord | None:
        record = self._state.ui_surfaces.get((tenant_id, surface_id))
        return None if record is None else _detached(record)

    async def get_ui_surface_revision(
        self, tenant_id: str, surface_id: str, revision: int
    ) -> UiSurfaceRevisionRecord:
        try:
            return _detached(self._state.ui_surface_revisions[(tenant_id, surface_id, revision)])
        except KeyError as error:
            raise _not_found("UI surface revision") from error

    async def get_action_proposal(self, tenant_id: str, action_ref: str) -> ActionProposalRecord:
        try:
            return _detached(self._state.action_proposals[(tenant_id, action_ref)])
        except KeyError as error:
            raise _not_found("action proposal") from error

    async def get_approval_request(
        self, tenant_id: str, approval_id: str
    ) -> ApprovalRequestRecord:
        try:
            return _detached(self._state.approvals[(tenant_id, approval_id)])
        except KeyError as error:
            raise _not_found("approval request") from error

    async def get_effect(self, tenant_id: str, effect_id: str) -> EffectLedgerRecord:
        try:
            return _detached(self._state.effects[(tenant_id, effect_id)])
        except KeyError as error:
            raise _not_found("effect") from error

    async def get_effect_by_key(
        self, tenant_id: str, effect_key: str
    ) -> EffectLedgerRecord | None:
        identity = self._state.effect_by_key.get((tenant_id, effect_key))
        if identity is None:
            return None
        return _detached(self._state.effects[identity])

    async def get_outbox_message(self, tenant_id: str, message_id: str) -> OutboxMessageRecord:
        try:
            return _detached(self._state.outbox[(tenant_id, message_id)])
        except KeyError as error:
            raise _not_found("outbox message") from error

    async def get_inbox_message(
        self, tenant_id: str, message_id: str, handler_version: str
    ) -> InboxMessageRecord:
        try:
            return _detached(self._state.inbox[(tenant_id, message_id, handler_version)])
        except KeyError as error:
            raise _not_found("inbox message") from error

    async def list_execution_units_for_run(
        self, tenant_id: str, run_id: str
    ) -> tuple[ExecutionUnitRecord, ...]:
        return tuple(
            _detached(record)
            for (record_tenant, _), record in self._state.units.items()
            if record_tenant == tenant_id and record.run_id == run_id
        )

    async def list_active_attempts_for_run(
        self, tenant_id: str, run_id: str
    ) -> tuple[AttemptRecord, ...]:
        return tuple(
            _detached(record)
            for (record_tenant, _), record in self._state.attempts.items()
            if record_tenant == tenant_id
            and record.run_id == run_id
            and record.status in ACTIVE_ATTEMPT_STATES
        )

    async def list_attempts_for_run(
        self, tenant_id: str, run_id: str
    ) -> tuple[AttemptRecord, ...]:
        return tuple(
            _detached(record)
            for (record_tenant, _), record in self._state.attempts.items()
            if record_tenant == tenant_id and record.run_id == run_id
        )

    async def list_active_leases_for_run(
        self, tenant_id: str, run_id: str
    ) -> tuple[ExecutionLeaseRecord, ...]:
        return tuple(
            _detached(record)
            for (record_tenant, _), record in self._state.leases.items()
            if record_tenant == tenant_id
            and record.run_id == run_id
            and record.state in ACTIVE_LEASE_STATES
        )

    async def list_events_for_run(
        self, tenant_id: str, run_id: str
    ) -> tuple[EnterpriseEventEnvelope, ...]:
        return tuple(_detached(self._state.events.get((tenant_id, run_id), [])))

    async def list_ui_surfaces_for_run(
        self, tenant_id: str, run_id: str
    ) -> tuple[UiSurfaceRecord, ...]:
        return tuple(
            _detached(record)
            for (record_tenant, _), record in self._state.ui_surfaces.items()
            if record_tenant == tenant_id and record.run_id == run_id
        )

    async def get_event_retention_floor(self, tenant_id: str, run_id: str) -> int:
        if (tenant_id, run_id) not in self._state.runs:
            raise _not_found("run")
        floor = self._retention_floor(tenant_id, run_id)
        if type(floor) is not int or floor < 0:
            raise PlatformError(
                "INTEGRITY_VIOLATION", "event retention floor must be a non-negative integer"
            )
        return floor

    async def list_schedulable_work(self) -> tuple[SchedulableWork, ...]:
        active_run_ids = {
            (attempt.tenant_id, attempt.run_id)
            for attempt in self._state.attempts.values()
            if attempt.status in ACTIVE_ATTEMPT_STATES
        }
        active_unit_ids = {
            (lease.tenant_id, lease.execution_unit_id)
            for lease in self._state.leases.values()
            if lease.state in ACTIVE_LEASE_STATES
        }
        candidates: list[SchedulableWork] = []
        for unit in self._state.units.values():
            run = self._state.runs.get((unit.tenant_id, unit.run_id))
            checkpoint = (
                None
                if unit.current_checkpoint_id is None
                else self._state.checkpoints.get((unit.tenant_id, unit.current_checkpoint_id))
            )
            if (
                run is None
                or checkpoint is None
                or run.status not in (RunState.QUEUED, RunState.RUNNING, RunState.RECOVERING)
                or unit.status not in {ExecutionUnitState.DISPATCHABLE, ExecutionUnitState.RECOVERING}
                or checkpoint.state is not CheckpointState.COMMITTED
                or (unit.tenant_id, unit.run_id) in active_run_ids
                or (unit.tenant_id, unit.execution_unit_id) in active_unit_ids
            ):
                continue
            candidates.append(
                SchedulableWork(
                    run=_detached(run),
                    unit=_detached(unit),
                    checkpoint=_detached(checkpoint),
                )
            )
        return tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item.run.tenant_id,
                    item.run.created_at,
                    item.run.run_id,
                    item.unit.execution_unit_id,
                ),
            )
        )

    async def claim_idempotency(
        self,
        tenant_id: str,
        namespace: str,
        idempotency_key: str,
        request_digest: str,
        actor_id: str,
        now: datetime,
    ) -> IdempotencyRecord | None:
        key = (tenant_id, namespace, idempotency_key)
        existing = self._state.idempotency.get(key)
        if existing is not None:
            if existing.request_digest != request_digest or existing.actor_id != actor_id:
                raise PlatformError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "idempotency key was already used for another request",
                )
            return _detached(existing)
        self._fault("claim_idempotency")
        self._state.idempotency[key] = IdempotencyRecord(
            tenant_id=tenant_id,
            namespace=namespace,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            actor_id=actor_id,
            status="IN_PROGRESS",
            result_type=None,
            result_id=None,
            result_schema=None,
            result_payload=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
        return None

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
        key = (tenant_id, namespace, idempotency_key)
        existing = self._state.idempotency.get(key)
        if existing is None or existing.request_digest != request_digest:
            raise PlatformError("INTEGRITY_VIOLATION", "idempotency claim is missing")
        if existing.status != "IN_PROGRESS":
            raise PlatformError("INTEGRITY_VIOLATION", "idempotency claim is already complete")
        if IDEMPOTENCY_RESULT_SCHEMAS.get(result_type) != result_schema:
            raise PlatformError(
                "INTEGRITY_VIOLATION", "idempotency result type and schema do not match"
            )
        canonical_payload = _canonical_json_object(result_payload)
        self._fault("complete_idempotency")
        completed = replace(
            existing,
            status="COMPLETED",
            result_type=result_type,
            result_id=result_id,
            result_schema=result_schema,
            result_payload=canonical_payload,
            version=existing.version + 1,
            updated_at=now,
        )
        self._state.idempotency[key] = _detached(completed)
        return _detached(completed)

    async def insert_run(self, record: RunRecord) -> None:
        self._fault("insert_run")
        key = (record.tenant_id, record.run_id)
        if key in self._state.runs:
            raise PlatformError("INTEGRITY_VIOLATION", "run already exists")
        self._state.runs[key] = _detached(record)

    async def insert_run_authorization_snapshot(
        self, record: RunAuthorizationSnapshotRecord
    ) -> None:
        self._fault("insert_authorization_snapshot")
        key = (record.tenant_id, record.run_id)
        if key in self._state.authorization_snapshots or key not in self._state.runs:
            raise PlatformError(
                "INTEGRITY_VIOLATION", "invalid or duplicate run authorization snapshot"
            )
        self._state.authorization_snapshots[key] = _detached(record)

    async def insert_execution_unit(self, record: ExecutionUnitRecord) -> None:
        self._fault("insert_execution_unit")
        key = (record.tenant_id, record.execution_unit_id)
        run_key = (record.tenant_id, record.run_id)
        if key in self._state.units or run_key not in self._state.runs:
            raise PlatformError("INTEGRITY_VIOLATION", "invalid execution unit relation")
        self._state.units[key] = _detached(record)
        if record.role == "primary":
            if run_key in self._state.primary_units:
                raise PlatformError("INTEGRITY_VIOLATION", "primary execution unit already exists")
            self._state.primary_units[run_key] = key

    async def insert_checkpoint(self, record: CheckpointRecord) -> None:
        self._fault("insert_checkpoint")
        key = (record.tenant_id, record.checkpoint_id)
        unit = self._state.units.get((record.tenant_id, record.execution_unit_id))
        source_attempt = (
            None
            if record.source_attempt_id is None
            else self._state.attempts.get((record.tenant_id, record.source_attempt_id))
        )
        if (
            key in self._state.checkpoints
            or unit is None
            or unit.run_id != record.run_id
            or (
                record.source_attempt_id is not None
                and (
                    source_attempt is None
                    or source_attempt.run_id != record.run_id
                    or source_attempt.execution_unit_id != record.execution_unit_id
                )
            )
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "invalid checkpoint relation")
        self._state.checkpoints[key] = _detached(record)

    async def insert_step(self, record: StepRecord) -> None:
        self._fault("insert_step")
        key = (record.tenant_id, record.step_id)
        run_key = (record.tenant_id, record.run_id)
        duplicate_ordinal = any(
            step.tenant_id == record.tenant_id
            and step.run_id == record.run_id
            and step.ordinal == record.ordinal
            for step in self._state.steps.values()
        )
        if key in self._state.steps or run_key not in self._state.runs or duplicate_ordinal:
            raise PlatformError("INTEGRITY_VIOLATION", "invalid step relation")
        self._state.steps[key] = _detached(record)

    async def insert_attempt(self, record: AttemptRecord) -> None:
        self._fault("insert_attempt")
        key = (record.tenant_id, record.attempt_id)
        unit = self._state.units.get((record.tenant_id, record.execution_unit_id))
        if key in self._state.attempts or unit is None or unit.run_id != record.run_id:
            raise PlatformError("INTEGRITY_VIOLATION", "invalid attempt relation")
        self._validate_attempt_uniqueness(record)
        self._state.attempts[key] = _detached(record)

    async def insert_lease(self, record: ExecutionLeaseRecord) -> None:
        self._fault("insert_lease")
        key = (record.tenant_id, record.lease_id)
        attempt_key = (record.tenant_id, record.attempt_id)
        attempt = self._state.attempts.get(attempt_key)
        if (
            key in self._state.leases
            or attempt_key in self._state.lease_by_attempt
            or attempt is None
            or attempt.run_id != record.run_id
            or attempt.execution_unit_id != record.execution_unit_id
            or attempt.generation != record.generation
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "invalid lease relation")
        self._validate_lease_uniqueness(record)
        self._state.leases[key] = _detached(record)
        self._state.lease_by_attempt[attempt_key] = key

    async def insert_workspace_snapshot(self, record: WorkspaceSnapshotRecord) -> None:
        self._fault("insert_workspace_snapshot")
        key = (record.tenant_id, record.snapshot_id)
        attempt = self._state.attempts.get((record.tenant_id, record.source_attempt_id))
        if (
            key in self._state.workspace_snapshots
            or attempt is None
            or attempt.run_id != record.run_id
            or attempt.execution_unit_id != record.execution_unit_id
            or attempt.generation != record.generation
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "invalid workspace snapshot relation")
        self._state.workspace_snapshots[key] = _detached(record)

    async def insert_artifact(self, record: ArtifactRecord) -> None:
        self._fault("insert_artifact")
        key = (record.tenant_id, record.artifact_id)
        if (
            key in self._state.artifacts
            or (record.tenant_id, record.run_id) not in self._state.runs
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "invalid artifact relation")
        self._state.artifacts[key] = _detached(record)

    async def insert_artifact_version(self, record: ArtifactVersionRecord) -> None:
        self._fault("insert_artifact_version")
        key = (record.tenant_id, record.artifact_id, record.version)
        artifact = self._state.artifacts.get((record.tenant_id, record.artifact_id))
        attempt = (
            None
            if record.source_attempt_id is None
            else self._state.attempts.get((record.tenant_id, record.source_attempt_id))
        )
        source_pair_valid = (record.source_attempt_id is None) == (record.generation is None)
        if (
            key in self._state.artifact_versions
            or artifact is None
            or artifact.run_id != record.run_id
            or not source_pair_valid
            or (record.source_attempt_id is not None and attempt is None)
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "invalid artifact version relation")
        self._state.artifact_versions[key] = _detached(record)

    async def insert_ui_surface(self, record: UiSurfaceRecord) -> None:
        self._fault("insert_ui_surface")
        key = (record.tenant_id, record.surface_id)
        if (
            key in self._state.ui_surfaces
            or (record.tenant_id, record.run_id) not in self._state.runs
            or record.current_revision is not None
            or record.version != 1
            or record.status != "ACTIVE"
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "invalid UI surface relation")
        self._state.ui_surfaces[key] = _detached(record)

    async def insert_ui_surface_revision(self, record: UiSurfaceRevisionRecord) -> None:
        self._fault("insert_ui_surface_revision")
        key = (record.tenant_id, record.surface_id, record.revision)
        surface = self._state.ui_surfaces.get((record.tenant_id, record.surface_id))
        attempt = self._state.attempts.get((record.tenant_id, record.source_attempt_id))
        events = self._state.events.get((record.tenant_id, record.run_id), [])
        source_event = next(
            (event for event in events if event.event_seq == record.source_event_seq),
            None,
        )
        if (
            key in self._state.ui_surface_revisions
            or surface is None
            or surface.run_id != record.run_id
            or attempt is None
            or attempt.run_id != record.run_id
            or attempt.generation != record.source_generation
            or source_event is None
            or source_event.attempt_id != record.source_attempt_id
            or source_event.payload.kind != "ui.surface.committed"
            or source_event.payload.surface_id != record.surface_id
            or source_event.payload.revision != record.revision
            or not record.checksum.startswith("sha256:")
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "invalid UI surface revision relation")
        self._state.ui_surface_revisions[key] = _detached(record)

    async def insert_action_proposal(self, record: ActionProposalRecord) -> None:
        self._fault("insert_action_proposal")
        validate_new_action_proposal(record)
        key = (record.tenant_id, record.action_ref)
        attempt = self._state.attempts.get((record.tenant_id, record.attempt_id))
        step = (
            None
            if record.step_id is None
            else self._state.steps.get((record.tenant_id, record.step_id))
        )
        if (
            key in self._state.action_proposals
            or attempt is None
            or attempt.run_id != record.run_id
            or attempt.execution_unit_id != record.execution_unit_id
            or attempt.generation != record.source_generation
            or (record.step_id is not None and (step is None or step.run_id != record.run_id))
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "invalid action proposal relation")
        self._state.action_proposals[key] = _detached(record)

    async def insert_approval_request(self, record: ApprovalRequestRecord) -> None:
        self._fault("insert_approval_request")
        key = (record.tenant_id, record.approval_id)
        proposal = self._state.action_proposals.get((record.tenant_id, record.action_ref))
        step = (
            None
            if record.step_id is None
            else self._state.steps.get((record.tenant_id, record.step_id))
        )
        if (
            key in self._state.approvals
            or proposal is None
            or proposal.run_id != record.run_id
            or proposal.request_digest != record.request_digest
            or proposal.step_id != record.step_id
            or (record.step_id is not None and (step is None or step.run_id != record.run_id))
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "invalid approval request relation")
        self._state.approvals[key] = _detached(record)

    async def insert_effect(self, record: EffectLedgerRecord) -> None:
        self._fault("insert_effect")
        key = (record.tenant_id, record.effect_id)
        effect_key = (record.tenant_id, record.effect_key)
        proposal = self._state.action_proposals.get((record.tenant_id, record.action_ref))
        approval = self._state.approvals.get((record.tenant_id, record.approval_id))
        if proposal is not None and approval is not None:
            validate_new_effect(record, proposal=proposal, approval=approval)
        if (
            key in self._state.effects
            or effect_key in self._state.effect_by_key
            or proposal is None
            or approval is None
            or proposal.run_id != record.run_id
            or approval.run_id != record.run_id
            or approval.action_ref != record.action_ref
            or approval.request_digest != record.request_digest
            or proposal.request_digest != record.request_digest
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "invalid effect relation")
        self._state.effects[key] = _detached(record)
        self._state.effect_by_key[effect_key] = key

    async def claim_inbox_message(self, record: InboxMessageRecord) -> bool:
        key = (record.tenant_id, record.message_id, record.handler_version)
        existing = self._state.inbox.get(key)
        if existing is not None:
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
        self._fault("claim_inbox_message")
        self._state.inbox[key] = _detached(record)
        return True

    async def replace_run_cas(self, record: RunRecord, expected_version: int) -> None:
        self._replace_cas(
            self._state.runs,
            (record.tenant_id, record.run_id),
            record,
            expected_version,
            "run",
        )

    async def replace_execution_unit_cas(
        self, record: ExecutionUnitRecord, expected_version: int
    ) -> None:
        self._replace_cas(
            self._state.units,
            (record.tenant_id, record.execution_unit_id),
            record,
            expected_version,
            "execution_unit",
        )

    async def replace_checkpoint_cas(
        self, record: CheckpointRecord, expected_version: int
    ) -> None:
        self._replace_cas(
            self._state.checkpoints,
            (record.tenant_id, record.checkpoint_id),
            record,
            expected_version,
            "checkpoint",
        )

    async def replace_step_cas(self, record: StepRecord, expected_version: int) -> None:
        self._replace_cas(
            self._state.steps,
            (record.tenant_id, record.step_id),
            record,
            expected_version,
            "step",
        )

    async def replace_attempt_cas(self, record: AttemptRecord, expected_version: int) -> None:
        key = (record.tenant_id, record.attempt_id)
        self._validate_cas_preconditions(
            self._state.attempts,
            key,
            record,
            expected_version,
            "attempt",
        )
        self._validate_attempt_uniqueness(record, exclude_attempt_id=record.attempt_id)
        self._write_cas(self._state.attempts, key, record, "attempt")

    async def replace_lease_cas(self, record: ExecutionLeaseRecord, expected_version: int) -> None:
        key = (record.tenant_id, record.lease_id)
        self._validate_cas_preconditions(
            self._state.leases,
            key,
            record,
            expected_version,
            "lease",
        )
        self._validate_lease_uniqueness(record, exclude_lease_id=record.lease_id)
        self._write_cas(self._state.leases, key, record, "lease")

    async def replace_inbox_message_cas(
        self, record: InboxMessageRecord, expected_version: int
    ) -> None:
        self._replace_cas(
            self._state.inbox,
            (record.tenant_id, record.message_id, record.handler_version),
            record,
            expected_version,
            "inbox_message",
        )

    async def replace_action_proposal_cas(
        self, record: ActionProposalRecord, expected_version: int
    ) -> None:
        key = (record.tenant_id, record.action_ref)
        current = self._state.action_proposals.get(key)
        if current is None:
            raise _not_found("action proposal")
        if replace(record, status=current.status, version=current.version) != current:
            raise PlatformError("INTEGRITY_VIOLATION", "action proposal facts are immutable")
        self._replace_cas(
            self._state.action_proposals,
            key,
            record,
            expected_version,
            "action_proposal",
        )

    async def replace_approval_request_cas(
        self, record: ApprovalRequestRecord, expected_version: int
    ) -> None:
        key = (record.tenant_id, record.approval_id)
        current = self._state.approvals.get(key)
        if current is None:
            raise _not_found("approval request")
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
        self._replace_cas(
            self._state.approvals,
            key,
            record,
            expected_version,
            "approval_request",
        )

    async def replace_effect_cas(self, record: EffectLedgerRecord, expected_version: int) -> None:
        key = (record.tenant_id, record.effect_id)
        current = self._state.effects.get(key)
        if current is None:
            raise _not_found("effect")
        if current.version != expected_version or record.version != expected_version + 1:
            raise PlatformError("VERSION_CONFLICT", "effect compare-and-swap failed")
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
        self._replace_cas(
            self._state.effects,
            key,
            record,
            expected_version,
            "effect",
        )

    async def replace_outbox_cas(self, record: OutboxMessageRecord, expected_version: int) -> None:
        key = (record.tenant_id, record.message_id)
        current = self._state.outbox.get(key)
        if current is None:
            raise _not_found("outbox message")
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
        if record.publish_state == "PUBLISHED" and record.published_at is None:
            raise PlatformError("INTEGRITY_VIOLATION", "published outbox requires time")
        if record.publish_state != "PUBLISHED" and record.published_at is not None:
            raise PlatformError("INTEGRITY_VIOLATION", "unpublished outbox cannot have publish time")
        self._replace_cas(
            self._state.outbox,
            key,
            record,
            expected_version,
            "outbox_message",
        )

    async def replace_ui_surface_cas(self, record: UiSurfaceRecord, expected_version: int) -> None:
        key = (record.tenant_id, record.surface_id)
        current = self._state.ui_surfaces.get(key)
        revision = (
            None
            if record.current_revision is None
            else self._state.ui_surface_revisions.get(
                (record.tenant_id, record.surface_id, record.current_revision)
            )
        )
        if (
            current is None
            or record.run_id != current.run_id
            or record.catalog_id != current.catalog_id
            or record.protocol_version != current.protocol_version
            or record.created_at != current.created_at
            or revision is None
            or revision.run_id != record.run_id
            or (
                current.current_revision is not None
                and record.current_revision <= current.current_revision
            )
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "invalid UI surface update")
        self._replace_cas(
            self._state.ui_surfaces,
            key,
            record,
            expected_version,
            "ui_surface",
        )

    def _replace_cas[T](
        self,
        table: dict[tuple[str, ...], T],
        key: tuple[str, ...],
        record: T,
        expected_version: int,
        operation: str,
    ) -> None:
        self._validate_cas_preconditions(
            table,
            key,
            record,
            expected_version,
            operation,
        )
        self._write_cas(table, key, record, operation)

    @staticmethod
    def _validate_cas_preconditions[T](
        table: dict[tuple[str, ...], T],
        key: tuple[str, ...],
        record: T,
        expected_version: int,
        operation: str,
    ) -> None:
        current = table.get(key)
        if current is None:
            raise _not_found(operation)
        current_version = current.version
        new_version = record.version
        if current_version != expected_version or new_version != expected_version + 1:
            raise PlatformError("VERSION_CONFLICT", f"{operation} compare-and-swap failed")

    def _write_cas[T](
        self,
        table: dict[tuple[str, ...], T],
        key: tuple[str, ...],
        record: T,
        operation: str,
    ) -> None:
        self._fault(f"replace_{operation}")
        table[key] = _detached(record)

    def _validate_attempt_uniqueness(
        self,
        record: AttemptRecord,
        exclude_attempt_id: str | None = None,
    ) -> None:
        same_unit = (
            current
            for (tenant_id, attempt_id), current in self._state.attempts.items()
            if tenant_id == record.tenant_id
            and current.execution_unit_id == record.execution_unit_id
            and attempt_id != exclude_attempt_id
        )
        for current in same_unit:
            if current.generation == record.generation:
                raise PlatformError(
                    "INTEGRITY_VIOLATION",
                    "attempt generation must be unique within an execution unit",
                )
            if record.status in ACTIVE_ATTEMPT_STATES and current.status in ACTIVE_ATTEMPT_STATES:
                raise PlatformError(
                    "INTEGRITY_VIOLATION",
                    "only one Attempt may be active within an execution unit",
                )

    def _validate_lease_uniqueness(
        self,
        record: ExecutionLeaseRecord,
        exclude_lease_id: str | None = None,
    ) -> None:
        if record.state not in ACTIVE_LEASE_STATES:
            return
        if any(
            tenant_id == record.tenant_id
            and lease_id != exclude_lease_id
            and current.execution_unit_id == record.execution_unit_id
            and current.state in ACTIVE_LEASE_STATES
            for (tenant_id, lease_id), current in self._state.leases.items()
        ):
            raise PlatformError(
                "INTEGRITY_VIOLATION",
                "only one Lease may be active within an execution unit",
            )

    async def append_event(
        self, event: EnterpriseEventEnvelope, expected_previous_seq: int
    ) -> None:
        self._fault("append_event")
        run_key = (event.tenant_id, event.run_id)
        run = self._state.runs.get(run_key)
        events = self._state.events.setdefault(run_key, [])
        actual_previous = events[-1].event_seq if events else 0
        if (
            run is None
            or actual_previous != expected_previous_seq
            or event.event_seq != expected_previous_seq + 1
            or any(item.event_id == event.event_id for item in events)
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "event sequence or relation is invalid")
        events.append(_detached(event))

    async def insert_outbox(self, record: OutboxMessageRecord) -> None:
        self._fault("insert_outbox")
        validate_new_outbox(record)
        key = (record.tenant_id, record.message_id)
        if key in self._state.outbox or (record.tenant_id, record.run_id) not in self._state.runs:
            raise PlatformError("INTEGRITY_VIOLATION", "invalid outbox relation")
        self._state.outbox[key] = _detached(record)

    async def insert_audit(self, record: AuditEventRecord) -> None:
        self._fault("insert_audit")
        key = (record.tenant_id, record.audit_event_id)
        if key in self._state.audit or (record.tenant_id, record.run_id) not in self._state.runs:
            raise PlatformError("INTEGRITY_VIOLATION", "invalid audit relation")
        self._state.audit[key] = _detached(record)

    def validate(self) -> None:
        for key, snapshot in self._state.authorization_snapshots.items():
            if key != (snapshot.tenant_id, snapshot.run_id) or key not in self._state.runs:
                raise PlatformError(
                    "INTEGRITY_VIOLATION", "run authorization snapshot relation is invalid"
                )
        for step in self._state.steps.values():
            if (step.tenant_id, step.run_id) not in self._state.runs:
                raise PlatformError("INTEGRITY_VIOLATION", "step relation is invalid")
        for run_key, run in self._state.runs.items():
            events = self._state.events.get(run_key, [])
            last_event_seq = events[-1].event_seq if events else 0
            if last_event_seq != run.last_event_seq:
                raise PlatformError(
                    "INTEGRITY_VIOLATION", "run watermark does not match durable events"
                )
        for unit_key, unit in self._state.units.items():
            if unit.current_checkpoint_id is None:
                continue
            checkpoint = self._state.checkpoints.get(
                (unit.tenant_id, unit.current_checkpoint_id)
            )
            if (
                checkpoint is None
                or checkpoint.execution_unit_id != unit.execution_unit_id
                or checkpoint.run_id != unit.run_id
                or unit_key != (checkpoint.tenant_id, checkpoint.execution_unit_id)
            ):
                raise PlatformError("INTEGRITY_VIOLATION", "unit checkpoint cursor is invalid")
        generation_keys: set[tuple[str, str, int]] = set()
        active_attempt_units: set[tuple[str, str]] = set()
        for attempt in self._state.attempts.values():
            if attempt.step_id is not None:
                step = self._state.steps.get((attempt.tenant_id, attempt.step_id))
                if step is None or step.run_id != attempt.run_id:
                    raise PlatformError("INTEGRITY_VIOLATION", "attempt step relation is invalid")
            generation_key = (
                attempt.tenant_id,
                attempt.execution_unit_id,
                attempt.generation,
            )
            if generation_key in generation_keys:
                raise PlatformError("INTEGRITY_VIOLATION", "duplicate Attempt generation")
            generation_keys.add(generation_key)
            if attempt.status in ACTIVE_ATTEMPT_STATES:
                unit_key = (attempt.tenant_id, attempt.execution_unit_id)
                if unit_key in active_attempt_units:
                    raise PlatformError("INTEGRITY_VIOLATION", "duplicate active Attempt")
                active_attempt_units.add(unit_key)
        active_lease_units: set[tuple[str, str]] = set()
        leased_attempts: set[tuple[str, str]] = set()
        for lease in self._state.leases.values():
            attempt_key = (lease.tenant_id, lease.attempt_id)
            if attempt_key in leased_attempts:
                raise PlatformError("INTEGRITY_VIOLATION", "Attempt has multiple Leases")
            leased_attempts.add(attempt_key)
            if lease.state in ACTIVE_LEASE_STATES:
                unit_key = (lease.tenant_id, lease.execution_unit_id)
                if unit_key in active_lease_units:
                    raise PlatformError("INTEGRITY_VIOLATION", "duplicate active Lease")
                active_lease_units.add(unit_key)
        for snapshot in self._state.workspace_snapshots.values():
            attempt = self._state.attempts.get((snapshot.tenant_id, snapshot.source_attempt_id))
            if (
                attempt is None
                or attempt.run_id != snapshot.run_id
                or attempt.execution_unit_id != snapshot.execution_unit_id
                or attempt.generation != snapshot.generation
            ):
                raise PlatformError("INTEGRITY_VIOLATION", "workspace snapshot relation is invalid")
        for version in self._state.artifact_versions.values():
            artifact = self._state.artifacts.get((version.tenant_id, version.artifact_id))
            if artifact is None or artifact.run_id != version.run_id:
                raise PlatformError("INTEGRITY_VIOLATION", "artifact version relation is invalid")
        for surface in self._state.ui_surfaces.values():
            if (surface.tenant_id, surface.run_id) not in self._state.runs:
                raise PlatformError("INTEGRITY_VIOLATION", "UI surface Run relation is invalid")
            if surface.current_revision is not None:
                revision = self._state.ui_surface_revisions.get(
                    (surface.tenant_id, surface.surface_id, surface.current_revision)
                )
                if revision is None or revision.run_id != surface.run_id:
                    raise PlatformError(
                        "INTEGRITY_VIOLATION", "UI surface current revision is invalid"
                    )
        for revision in self._state.ui_surface_revisions.values():
            surface = self._state.ui_surfaces.get((revision.tenant_id, revision.surface_id))
            attempt = self._state.attempts.get((revision.tenant_id, revision.source_attempt_id))
            if (
                surface is None
                or surface.run_id != revision.run_id
                or attempt is None
                or attempt.run_id != revision.run_id
                or attempt.generation != revision.source_generation
            ):
                raise PlatformError("INTEGRITY_VIOLATION", "UI surface revision relation is invalid")
        for approval in self._state.approvals.values():
            proposal = self._state.action_proposals.get((approval.tenant_id, approval.action_ref))
            if (
                proposal is None
                or proposal.run_id != approval.run_id
                or proposal.request_digest != approval.request_digest
            ):
                raise PlatformError("INTEGRITY_VIOLATION", "approval request relation is invalid")
        for key, effect in self._state.effects.items():
            proposal = self._state.action_proposals.get((effect.tenant_id, effect.action_ref))
            approval = self._state.approvals.get((effect.tenant_id, effect.approval_id))
            if (
                key != (effect.tenant_id, effect.effect_id)
                or self._state.effect_by_key.get((effect.tenant_id, effect.effect_key)) != key
                or proposal is None
                or approval is None
                or proposal.run_id != effect.run_id
                or approval.run_id != effect.run_id
                or approval.action_ref != effect.action_ref
                or approval.request_digest != effect.request_digest
            ):
                raise PlatformError("INTEGRITY_VIOLATION", "effect ledger relation is invalid")


class InMemoryPlatformStore:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
        fault_injector: Callable[[str], None] | None = None,
        retention_floor: Callable[[str, str], int] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda kind: f"{kind}_{uuid4().hex}")
        self._fault_injector = fault_injector
        self._retention_floor = retention_floor or (lambda tenant_id, run_id: 0)
        self._lock = asyncio.Lock()
        self._state = _State()

    def new_id(self, kind: str) -> str:
        return self._id_factory(kind)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[PlatformTransaction]:
        async with self._lock:
            working = _detached(self._state)
            transaction = _MemoryTransaction(
                working,
                self._clock,
                self._fault_injector,
                self._retention_floor,
            )
            yield transaction
            transaction.validate()
            self._state = working

    async def _read[T](self, reader: Callable[[_MemoryTransaction], T]) -> T:
        async with self._lock:
            transaction = _MemoryTransaction(
                self._state,
                self._clock,
                None,
                self._retention_floor,
            )
            return _detached(reader(transaction))

    async def get_run(self, tenant_id: str, run_id: str) -> RunRecord:
        async with self._lock:
            try:
                return _detached(self._state.runs[(tenant_id, run_id)])
            except KeyError as error:
                raise _not_found("run") from error

    async def get_run_authorization_snapshot(
        self, tenant_id: str, run_id: str
    ) -> RunAuthorizationSnapshotRecord:
        async with self._lock:
            try:
                return _detached(self._state.authorization_snapshots[(tenant_id, run_id)])
            except KeyError as error:
                raise _not_found("run authorization snapshot") from error

    async def get_primary_unit(self, tenant_id: str, run_id: str) -> ExecutionUnitRecord:
        async with self._lock:
            try:
                key = self._state.primary_units[(tenant_id, run_id)]
                return _detached(self._state.units[key])
            except KeyError as error:
                raise _not_found("primary execution unit") from error

    async def get_execution_unit(
        self, tenant_id: str, execution_unit_id: str
    ) -> ExecutionUnitRecord:
        async with self._lock:
            try:
                return _detached(self._state.units[(tenant_id, execution_unit_id)])
            except KeyError as error:
                raise _not_found("execution unit") from error

    async def get_checkpoint(self, tenant_id: str, checkpoint_id: str) -> CheckpointRecord:
        async with self._lock:
            try:
                return _detached(self._state.checkpoints[(tenant_id, checkpoint_id)])
            except KeyError as error:
                raise _not_found("checkpoint") from error

    async def get_step(self, tenant_id: str, step_id: str) -> StepRecord:
        async with self._lock:
            try:
                return _detached(self._state.steps[(tenant_id, step_id)])
            except KeyError as error:
                raise _not_found("step") from error

    async def get_attempt(self, tenant_id: str, attempt_id: str) -> AttemptRecord:
        async with self._lock:
            try:
                return _detached(self._state.attempts[(tenant_id, attempt_id)])
            except KeyError as error:
                raise _not_found("attempt") from error

    async def get_lease_for_attempt(
        self, tenant_id: str, attempt_id: str
    ) -> ExecutionLeaseRecord:
        async with self._lock:
            try:
                key = self._state.lease_by_attempt[(tenant_id, attempt_id)]
                return _detached(self._state.leases[key])
            except KeyError as error:
                raise _not_found("execution lease") from error

    async def get_workspace_snapshot(
        self, tenant_id: str, snapshot_id: str
    ) -> WorkspaceSnapshotRecord:
        async with self._lock:
            try:
                return _detached(self._state.workspace_snapshots[(tenant_id, snapshot_id)])
            except KeyError as error:
                raise _not_found("workspace snapshot") from error

    async def get_artifact_version(
        self, tenant_id: str, artifact_id: str, version: int
    ) -> ArtifactVersionRecord:
        async with self._lock:
            try:
                return _detached(self._state.artifact_versions[(tenant_id, artifact_id, version)])
            except KeyError as error:
                raise _not_found("artifact version") from error

    async def get_ui_surface(self, tenant_id: str, surface_id: str) -> UiSurfaceRecord | None:
        async with self._lock:
            record = self._state.ui_surfaces.get((tenant_id, surface_id))
            return None if record is None else _detached(record)

    async def get_ui_surface_revision(
        self, tenant_id: str, surface_id: str, revision: int
    ) -> UiSurfaceRevisionRecord:
        async with self._lock:
            try:
                return _detached(
                    self._state.ui_surface_revisions[(tenant_id, surface_id, revision)]
                )
            except KeyError as error:
                raise _not_found("UI surface revision") from error

    async def get_action_proposal(self, tenant_id: str, action_ref: str) -> ActionProposalRecord:
        async with self._lock:
            try:
                return _detached(self._state.action_proposals[(tenant_id, action_ref)])
            except KeyError as error:
                raise _not_found("action proposal") from error

    async def get_approval_request(
        self, tenant_id: str, approval_id: str
    ) -> ApprovalRequestRecord:
        async with self._lock:
            try:
                return _detached(self._state.approvals[(tenant_id, approval_id)])
            except KeyError as error:
                raise _not_found("approval request") from error

    async def get_effect(self, tenant_id: str, effect_id: str) -> EffectLedgerRecord:
        async with self._lock:
            try:
                return _detached(self._state.effects[(tenant_id, effect_id)])
            except KeyError as error:
                raise _not_found("effect") from error

    async def get_effect_by_key(
        self, tenant_id: str, effect_key: str
    ) -> EffectLedgerRecord | None:
        async with self._lock:
            identity = self._state.effect_by_key.get((tenant_id, effect_key))
            return None if identity is None else _detached(self._state.effects[identity])

    async def get_outbox_message(self, tenant_id: str, message_id: str) -> OutboxMessageRecord:
        async with self._lock:
            try:
                return _detached(self._state.outbox[(tenant_id, message_id)])
            except KeyError as error:
                raise _not_found("outbox message") from error

    async def get_inbox_message(
        self, tenant_id: str, message_id: str, handler_version: str
    ) -> InboxMessageRecord:
        async with self._lock:
            try:
                return _detached(self._state.inbox[(tenant_id, message_id, handler_version)])
            except KeyError as error:
                raise _not_found("inbox message") from error

    async def list_schedulable_work(self) -> tuple[SchedulableWork, ...]:
        async with self._lock:
            transaction = _MemoryTransaction(
                self._state,
                self._clock,
                None,
                self._retention_floor,
            )
            return _detached(await transaction.list_schedulable_work())

    async def list_events(
        self, tenant_id: str, run_id: str
    ) -> tuple[EnterpriseEventEnvelope, ...]:
        async with self._lock:
            return tuple(_detached(self._state.events.get((tenant_id, run_id), [])))

    async def list_runs(self, tenant_id: str) -> tuple[RunRecord, ...]:
        async with self._lock:
            return tuple(
                _detached(record)
                for (record_tenant, _), record in self._state.runs.items()
                if record_tenant == tenant_id
            )

    async def list_authorization_snapshots(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[RunAuthorizationSnapshotRecord, ...]:
        async with self._lock:
            return tuple(
                _detached(record)
                for (record_tenant, _), record in self._state.authorization_snapshots.items()
                if record_tenant == tenant_id and (run_id is None or record.run_id == run_id)
            )

    async def list_execution_units(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[ExecutionUnitRecord, ...]:
        async with self._lock:
            return tuple(
                _detached(record)
                for (record_tenant, _), record in self._state.units.items()
                if record_tenant == tenant_id and (run_id is None or record.run_id == run_id)
            )

    async def list_checkpoints(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[CheckpointRecord, ...]:
        async with self._lock:
            return tuple(
                _detached(record)
                for (record_tenant, _), record in self._state.checkpoints.items()
                if record_tenant == tenant_id and (run_id is None or record.run_id == run_id)
            )

    async def list_attempts(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[AttemptRecord, ...]:
        async with self._lock:
            return tuple(
                _detached(record)
                for (record_tenant, _), record in self._state.attempts.items()
                if record_tenant == tenant_id and (run_id is None or record.run_id == run_id)
            )

    async def list_leases(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[ExecutionLeaseRecord, ...]:
        async with self._lock:
            return tuple(
                _detached(record)
                for (record_tenant, _), record in self._state.leases.items()
                if record_tenant == tenant_id and (run_id is None or record.run_id == run_id)
            )

    async def list_ui_surfaces(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[UiSurfaceRecord, ...]:
        async with self._lock:
            return tuple(
                _detached(record)
                for (record_tenant, _), record in self._state.ui_surfaces.items()
                if record_tenant == tenant_id and (run_id is None or record.run_id == run_id)
            )

    async def list_outbox(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[OutboxMessageRecord, ...]:
        async with self._lock:
            return tuple(
                _detached(record)
                for (record_tenant, _), record in self._state.outbox.items()
                if record_tenant == tenant_id and (run_id is None or record.run_id == run_id)
            )

    async def list_pending_outbox(self, *, limit: int = 100) -> tuple[OutboxMessageRecord, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("outbox limit must be between 1 and 1000")
        async with self._lock:
            now = self._clock()
            candidates = (
                _detached(record)
                for record in self._state.outbox.values()
                if record.publish_state == "PENDING"
                and (record.next_attempt_at is None or record.next_attempt_at <= now)
            )
            return tuple(
                sorted(
                    candidates,
                    key=lambda item: (item.created_at, item.tenant_id, item.message_id),
                )[:limit]
            )

    async def list_audit_events(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[AuditEventRecord, ...]:
        async with self._lock:
            return tuple(
                _detached(record)
                for (record_tenant, _), record in self._state.audit.items()
                if record_tenant == tenant_id and (run_id is None or record.run_id == run_id)
            )

    async def list_approval_requests(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[ApprovalRequestRecord, ...]:
        async with self._lock:
            return tuple(
                _detached(record)
                for (record_tenant, _), record in self._state.approvals.items()
                if record_tenant == tenant_id and (run_id is None or record.run_id == run_id)
            )

    async def list_effects(
        self, tenant_id: str, run_id: str | None = None
    ) -> tuple[EffectLedgerRecord, ...]:
        async with self._lock:
            return tuple(
                _detached(record)
                for (record_tenant, _), record in self._state.effects.items()
                if record_tenant == tenant_id and (run_id is None or record.run_id == run_id)
            )

    async def list_idempotency_records(self, tenant_id: str) -> tuple[IdempotencyRecord, ...]:
        async with self._lock:
            return tuple(
                _detached(record)
                for (record_tenant, _, _), record in self._state.idempotency.items()
                if record_tenant == tenant_id
            )
