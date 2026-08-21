"""Exact, version-independent domain enumeration values."""
from enum import Enum


class StringEnum(str, Enum):
    pass


class RunState(StringEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RECOVERING = "RECOVERING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepState(StringEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class ExecutionUnitState(StringEnum):
    IDLE = "IDLE"
    DISPATCHABLE = "DISPATCHABLE"
    EXECUTING = "EXECUTING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    RECOVERING = "RECOVERING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AttemptState(StringEnum):
    CREATED = "CREATED"
    PROVISIONING = "PROVISIONING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    CHECKPOINTING = "CHECKPOINTING"
    CHECKPOINTED_FOR_APPROVAL = "CHECKPOINTED_FOR_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    LOST = "LOST"
    CANCELLED = "CANCELLED"


class ExecutionLeaseState(StringEnum):
    RESERVED = "RESERVED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    RELEASED = "RELEASED"
    REVOKED = "REVOKED"


class CheckpointState(StringEnum):
    PREPARING = "PREPARING"
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"


class WorkspaceSnapshotState(StringEnum):
    CREATING = "CREATING"
    SCANNING = "SCANNING"
    READY = "READY"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    DELETED = "DELETED"


class ArtifactVersionState(StringEnum):
    STAGING = "STAGING"
    SCANNING = "SCANNING"
    READY = "READY"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    DELETED = "DELETED"


class ToolGrantState(StringEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class ToolInvocationState(StringEnum):
    CREATED = "CREATED"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    UNKNOWN = "UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class ActionProposalState(StringEnum):
    OPEN = "OPEN"
    CONSUMED = "CONSUMED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


class ApprovalState(StringEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class EffectState(StringEnum):
    PREPARED = "PREPARED"
    EXECUTING = "EXECUTING"
    UNKNOWN = "UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ToolRiskClass(StringEnum):
    LOCAL = "LOCAL"
    READ = "READ"
    WRITE = "WRITE"


class RuntimeCapabilityAudience(StringEnum):
    TOOL_GATEWAY = "tool-gateway"


class EffectCapabilityAudience(StringEnum):
    EFFECT_EXECUTOR = "effect-executor"


class EventType(StringEnum):
    RUN_CREATED = "run.created"
    RUN_STATUS_CHANGED = "run.status.changed"
    ATTEMPT_LIFECYCLE = "attempt.lifecycle"
    TOOL_INVOCATION_RECORDED = "tool.invocation.recorded"
    APPROVAL_DECIDED = "approval.decided"
    EFFECT_STATUS_CHANGED = "effect.status.changed"
    UI_SURFACE_COMMITTED = "ui.surface.committed"
    ARTIFACT_VERSION = "artifact.version"
    ACTION_PROPOSAL = "action.proposal"


class EntityType(StringEnum):
    RUN = "run"
    STEP = "step"
    EXECUTION_UNIT = "execution_unit"
    ATTEMPT = "attempt"
    EXECUTION_LEASE = "execution_lease"
    CHECKPOINT = "checkpoint"
    WORKSPACE_SNAPSHOT = "workspace_snapshot"
    ARTIFACT_VERSION = "artifact_version"
    TOOL_GRANT = "tool_grant"
    TOOL_INVOCATION = "tool_invocation"
    ACTION_PROPOSAL = "action_proposal"
    APPROVAL = "approval"
    EFFECT_LEDGER = "effect_ledger"
