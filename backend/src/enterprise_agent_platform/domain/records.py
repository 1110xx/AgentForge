"""Immutable persistence records for stable business facts.

Records intentionally contain no runner process or Kubernetes Pod identity. Those
are replaceable execution details, while these records survive retries and recovery.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import JsonValue

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


@dataclass(frozen=True, slots=True)
class RunRecord:
    tenant_id: str
    run_id: str
    owner_id: str
    parent_run_id: str | None
    workflow_type: str
    intent: str
    resource_refs: tuple[str, ...]
    parameters: dict[str, JsonValue]
    host_context_ref: str | None
    status: RunState
    status_reason: str | None
    version: int
    last_event_seq: int
    fsm_version: str
    cancel_requested_by: str | None
    cancel_requested_at: datetime | None
    cancel_reason: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class RunAuthorizationSnapshotRecord:
    """Authority facts frozen when a host accepts a Run.

    Values are canonical identifiers and digests only. Credentials, bearer tokens,
    endpoints and caller-selected URLs are deliberately not representable here.
    """
    tenant_id: str
    run_id: str
    resolved_resources: tuple[dict[str, JsonValue], ...]
    host_context_digest: str | None
    host_context_version: str | None
    policy_digest: str
    policy_version: str
    policy_scopes: tuple[str, ...]
    policy_budget: dict[str, JsonValue]
    snapshot_digest: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunAuthorizationContext:
    """Canonical host authority facts resolved before execution."""
    resolved_resources: tuple[dict[str, JsonValue], ...]
    host_context_digest: str | None
    host_context_version: str | None
    policy_digest: str
    policy_version: str
    policy_scopes: tuple[str, ...]
    policy_budget: dict[str, JsonValue]
    snapshot_digest: str


@dataclass(frozen=True, slots=True)
class ExecutionUnitRecord:
    tenant_id: str
    execution_unit_id: str
    run_id: str
    role: str
    status: ExecutionUnitState
    version: int
    current_checkpoint_id: str | None
    next_generation: int
    runtime_profile: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    tenant_id: str
    checkpoint_id: str
    run_id: str
    execution_unit_id: str
    source_attempt_id: str | None
    checkpoint_seq: int
    state: CheckpointState
    workflow_cursor: dict[str, JsonValue]
    last_event_seq: int
    workspace_snapshot_id: str | None
    checkpoint_schema_version: str
    runtime_profile_version: str
    policy_version: str
    tool_catalog_version: str
    ui_catalog_version: str
    checksum: str
    version: int
    created_at: datetime
    committed_at: datetime | None
    completed_step_ids: tuple[str, ...]
    active_step_context: dict[str, JsonValue] = field(default_factory=dict)
    input_artifact_versions: tuple[dict[str, JsonValue], ...] = ()
    output_artifact_versions: tuple[dict[str, JsonValue], ...] = ()
    resolved_tool_call_ids: tuple[str, ...] = ()
    effect_states: dict[str, JsonValue] = field(default_factory=dict)
    budget_consumed: dict[str, JsonValue] = field(default_factory=dict)
    model_context_summary_ref: str | None = None
    runtime_image_digest: str | None = None
    agent_state: dict[str, JsonValue] = field(default_factory=dict)
    agent_state_schema_version: str = "pi-agent-core/v1"


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    tenant_id: str
    attempt_id: str
    run_id: str
    execution_unit_id: str
    step_id: str | None
    generation: int
    status: AttemptState
    version: int
    runtime_profile: str
    source_checkpoint_id: str | None
    reservation_key: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    failure_id: str | None


@dataclass(frozen=True, slots=True)
class ExecutionLeaseRecord:
    tenant_id: str
    lease_id: str
    run_id: str
    execution_unit_id: str
    attempt_id: str
    generation: int
    state: ExecutionLeaseState
    owner: str | None
    version: int
    activated_from_version: int | None
    provision_deadline: datetime
    heartbeat_at: datetime | None
    expires_at: datetime | None
    released_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StepRecord:
    tenant_id: str
    step_id: str
    run_id: str
    ordinal: int
    name: str
    step_type: str
    policy_snapshot: dict[str, JsonValue]
    status: StepState
    status_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshotRecord:
    tenant_id: str
    snapshot_id: str
    run_id: str
    source_attempt_id: str
    execution_unit_id: str
    generation: int
    state: WorkspaceSnapshotState
    manifest_uri: str
    checksum: str
    size_bytes: int
    runtime_image_digest: str
    version: int
    created_at: datetime
    ready_at: datetime | None


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    tenant_id: str
    artifact_id: str
    run_id: str
    logical_name: str
    artifact_type: str
    classification: str
    retention_policy: dict[str, JsonValue]
    state: str
    current_version: int | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactVersionRecord:
    tenant_id: str
    artifact_id: str
    version: int
    run_id: str
    source_attempt_id: str | None
    generation: int | None
    state: ArtifactVersionState
    state_version: int
    object_uri: str
    checksum: str
    size_bytes: int
    media_type: str
    lineage: dict[str, JsonValue]
    created_at: datetime
    ready_at: datetime | None


@dataclass(frozen=True, slots=True)
class UiSurfaceRecord:
    tenant_id: str
    surface_id: str
    run_id: str
    catalog_id: str
    protocol_version: str
    current_revision: int | None
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UiSurfaceRevisionRecord:
    tenant_id: str
    surface_id: str
    revision: int
    run_id: str
    source_attempt_id: str
    source_generation: int
    source_event_seq: int
    document: dict[str, JsonValue]
    checksum: str
    validation_result: dict[str, JsonValue]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ActionProposalRecord:
    tenant_id: str
    action_ref: str
    run_id: str
    step_id: str | None
    attempt_id: str
    execution_unit_id: str
    source_generation: int
    tool_name: str
    tool_spec_version: str
    tool_spec_digest: str
    connector_name: str
    required_scopes: tuple[str, ...]
    request_digest: str
    canonical_payload_digest: str
    canonical_target: str
    risk_class: str
    status: ActionProposalState
    version: int
    payload_ref: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalRequestRecord:
    tenant_id: str
    approval_id: str
    run_id: str
    step_id: str | None
    action_ref: str
    approval_type: str
    request_digest: str
    status: ApprovalState
    version: int
    canonical_request_ref: str
    expires_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_reason: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EffectLedgerRecord:
    tenant_id: str
    effect_id: str
    run_id: str
    action_ref: str
    approval_id: str
    effect_key: str
    request_digest: str
    tool_name: str
    tool_version: str
    tool_spec_digest: str
    connector_name: str
    required_scopes: tuple[str, ...]
    canonical_target: str
    canonical_payload_digest: str
    state: EffectState
    version: int
    executor_id: str | None
    execution_epoch: int
    executor_lease_expires_at: datetime | None
    result_ref: str | None
    remote_operation_id: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class InboxMessageRecord:
    tenant_id: str
    message_id: str
    handler_version: str
    topic: str
    payload_schema: str
    payload_digest: str
    processing_state: str
    version: int
    received_at: datetime
    processed_at: datetime | None
    failure_code: str | None


# Retention horizon for idempotency claims: after this window a key may be
# recycled by a new request and expired rows are purgeable (SDD §13.2 risk:
# IdempotencyRecord used to accumulate forever).
IDEMPOTENCY_RETENTION = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    tenant_id: str
    namespace: str
    idempotency_key: str
    request_digest: str
    actor_id: str
    status: str
    result_type: str | None
    result_id: str | None
    result_schema: str | None
    result_payload: dict[str, JsonValue] | None
    version: int
    created_at: datetime
    updated_at: datetime
    # Claimed keys carry a horizon from ``now``; completion refreshes it so a
    # completed result stays replayable for a full retention window. ``None``
    # (legacy rows) is treated as never-expired.
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FollowupRequestRecord:
    """A durable follow-up question awaiting execution by a new Attempt.

    ``status`` is one of ``PENDING`` / ``ANSWERED``. The question is read by
    the parent orchestrator during ``restore`` and injected into the child
    Runner's workflow cursor; the answer is written back by the orchestrator
    when the follow-up Attempt commits.
    """

    tenant_id: str
    followup_id: str
    run_id: str
    question: str
    client_followup_id: str
    status: str
    answer: str | None
    version: int
    created_at: datetime
    answered_at: datetime | None


@dataclass(frozen=True, slots=True)
class OutboxMessageRecord:
    tenant_id: str
    message_id: str
    run_id: str
    topic: str
    payload: dict[str, JsonValue]
    event_id: str | None
    aggregate_version: int
    created_at: datetime
    published_at: datetime | None
    publish_state: str = "PENDING"
    version: int = 1
    delivery_attempts: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    last_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class AuditEventRecord:
    tenant_id: str
    audit_event_id: str
    run_id: str
    actor_id: str
    action: str
    entity_type: str
    entity_id: str
    entity_version: int
    outcome: str
    trace_id: str | None
    details: dict[str, JsonValue]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AttemptReservation:
    attempt: AttemptRecord
    lease: ExecutionLeaseRecord


@dataclass(frozen=True, slots=True)
class SchedulableWork:
    run: RunRecord
    unit: ExecutionUnitRecord
    checkpoint: CheckpointRecord


@dataclass(frozen=True, slots=True)
class DispatchTicket:
    worker_id: str
    tenant_id: str
    run_id: str
    execution_unit_id: str
    attempt_id: str
    lease_id: str
    generation: int
    source_checkpoint_id: str


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    expired_attempt: AttemptRecord
    expired_lease: ExecutionLeaseRecord
    successor_attempt: AttemptRecord
    successor_lease: ExecutionLeaseRecord
