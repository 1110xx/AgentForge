"""Explicit, persistence-free finite state machine transition rules."""
from dataclasses import dataclass
from enum import Enum

from enterprise_agent_platform.contracts.enums import (
    ActionProposalState,
    ApprovalState,
    ArtifactVersionState,
    AttemptState,
    CheckpointState,
    EffectState,
    EntityType,
    ExecutionLeaseState,
    ExecutionUnitState,
    RunState,
    StepState,
    ToolGrantState,
    ToolInvocationState,
    WorkspaceSnapshotState,
)
from enterprise_agent_platform.contracts.errors import InvalidTransitionError


@dataclass(frozen=True)
class CompleteCancellation:
    audit_event_id: str


TransitionTable = dict[EntityType, dict[Enum, set[Enum]]]

STATE_TYPES: dict[EntityType, type[Enum]] = {
    EntityType.RUN: RunState,
    EntityType.STEP: StepState,
    EntityType.EXECUTION_UNIT: ExecutionUnitState,
    EntityType.ATTEMPT: AttemptState,
    EntityType.EXECUTION_LEASE: ExecutionLeaseState,
    EntityType.CHECKPOINT: CheckpointState,
    EntityType.WORKSPACE_SNAPSHOT: WorkspaceSnapshotState,
    EntityType.ARTIFACT_VERSION: ArtifactVersionState,
    EntityType.TOOL_GRANT: ToolGrantState,
    EntityType.TOOL_INVOCATION: ToolInvocationState,
    EntityType.ACTION_PROPOSAL: ActionProposalState,
    EntityType.APPROVAL: ApprovalState,
    EntityType.EFFECT_LEDGER: EffectState,
}

TRANSITIONS: TransitionTable = {
    EntityType.RUN: {
        RunState.QUEUED: {RunState.RUNNING, RunState.CANCEL_REQUESTED, RunState.FAILED},
        RunState.RUNNING: {
            RunState.WAITING_APPROVAL,
            RunState.RECOVERING,
            RunState.CANCEL_REQUESTED,
            RunState.NEEDS_ATTENTION,
            RunState.SUCCEEDED,
            RunState.FAILED,
        },
        RunState.WAITING_APPROVAL: {
            RunState.RUNNING,
            RunState.RECOVERING,
            RunState.CANCEL_REQUESTED,
            RunState.NEEDS_ATTENTION,
            RunState.FAILED,
        },
        RunState.RECOVERING: {
            RunState.RUNNING,
            RunState.CANCEL_REQUESTED,
            RunState.NEEDS_ATTENTION,
            RunState.FAILED,
        },
        RunState.NEEDS_ATTENTION: {
            RunState.RECOVERING,
            RunState.CANCEL_REQUESTED,
            RunState.FAILED,
            RunState.CANCELLED,
        },
        RunState.CANCEL_REQUESTED: {RunState.CANCELLED, RunState.NEEDS_ATTENTION},
        # Follow-up reactivation: a terminal Run may be reopened for a new
        # Attempt that answers a follow-up question (see §6.4 in SDD).
        # The Run returns to SUCCEEDED once the follow-up Attempt commits.
        RunState.SUCCEEDED: {RunState.RECOVERING},
        RunState.FAILED: {RunState.RECOVERING},
    },
    EntityType.STEP: {
        StepState.PENDING: {StepState.ACTIVE, StepState.SKIPPED, StepState.CANCELLED},
        StepState.ACTIVE: {
            StepState.WAITING_APPROVAL,
            StepState.SUCCEEDED,
            StepState.FAILED,
            StepState.CANCELLED,
        },
        StepState.WAITING_APPROVAL: {
            StepState.ACTIVE,
            StepState.NEEDS_ATTENTION,
            StepState.SKIPPED,
            StepState.FAILED,
            StepState.CANCELLED,
        },
        StepState.NEEDS_ATTENTION: {
            StepState.ACTIVE,
            StepState.FAILED,
            StepState.CANCELLED,
        },
    },
    EntityType.EXECUTION_UNIT: {
        ExecutionUnitState.IDLE: {ExecutionUnitState.DISPATCHABLE, ExecutionUnitState.CANCELLED},
        ExecutionUnitState.DISPATCHABLE: {
            ExecutionUnitState.EXECUTING,
            ExecutionUnitState.RECOVERING,
            ExecutionUnitState.CANCELLED,
            ExecutionUnitState.FAILED,
        },
        ExecutionUnitState.EXECUTING: {
            ExecutionUnitState.WAITING_APPROVAL,
            ExecutionUnitState.RECOVERING,
            ExecutionUnitState.SUCCEEDED,
            ExecutionUnitState.FAILED,
            ExecutionUnitState.CANCELLED,
        },
        ExecutionUnitState.WAITING_APPROVAL: {
            ExecutionUnitState.NEEDS_ATTENTION,
            ExecutionUnitState.RECOVERING,
            ExecutionUnitState.FAILED,
            ExecutionUnitState.CANCELLED,
        },
        ExecutionUnitState.NEEDS_ATTENTION: {
            ExecutionUnitState.RECOVERING,
            ExecutionUnitState.FAILED,
            ExecutionUnitState.CANCELLED,
        },
        ExecutionUnitState.RECOVERING: {
            ExecutionUnitState.DISPATCHABLE,
            ExecutionUnitState.EXECUTING,
            ExecutionUnitState.FAILED,
            ExecutionUnitState.CANCELLED,
        },
        # Follow-up reactivation: terminal units reopen for a follow-up Attempt.
        ExecutionUnitState.SUCCEEDED: {ExecutionUnitState.RECOVERING},
        ExecutionUnitState.FAILED: {ExecutionUnitState.RECOVERING},
    },
    EntityType.ATTEMPT: {
        AttemptState.CREATED: {AttemptState.PROVISIONING, AttemptState.CANCELLED},
        AttemptState.PROVISIONING: {
            AttemptState.CLAIMED,
            AttemptState.FAILED,
            AttemptState.LOST,
            AttemptState.CANCELLED,
        },
        AttemptState.CLAIMED: {
            AttemptState.RUNNING,
            AttemptState.FAILED,
            AttemptState.LOST,
            AttemptState.CANCELLED,
        },
        AttemptState.RUNNING: {
            AttemptState.CHECKPOINTING,
            AttemptState.SUCCEEDED,
            AttemptState.FAILED,
            AttemptState.LOST,
            AttemptState.CANCELLED,
        },
        AttemptState.CHECKPOINTING: {
            AttemptState.RUNNING,
            AttemptState.CHECKPOINTED_FOR_APPROVAL,
            AttemptState.SUCCEEDED,
            AttemptState.FAILED,
            AttemptState.LOST,
            AttemptState.CANCELLED,
        },
    },
    EntityType.EXECUTION_LEASE: {
        ExecutionLeaseState.RESERVED: {
            ExecutionLeaseState.ACTIVE,
            ExecutionLeaseState.EXPIRED,
            ExecutionLeaseState.REVOKED,
            ExecutionLeaseState.RELEASED,
        },
        ExecutionLeaseState.ACTIVE: {
            ExecutionLeaseState.RELEASED,
            ExecutionLeaseState.EXPIRED,
            ExecutionLeaseState.REVOKED,
        },
    },
    EntityType.CHECKPOINT: {
        CheckpointState.PREPARING: {CheckpointState.COMMITTED, CheckpointState.ABORTED},
    },
    EntityType.WORKSPACE_SNAPSHOT: {
        WorkspaceSnapshotState.CREATING: {
            WorkspaceSnapshotState.SCANNING,
            WorkspaceSnapshotState.FAILED,
        },
        WorkspaceSnapshotState.SCANNING: {
            WorkspaceSnapshotState.READY,
            WorkspaceSnapshotState.REJECTED,
            WorkspaceSnapshotState.FAILED,
        },
        WorkspaceSnapshotState.READY: {WorkspaceSnapshotState.DELETED},
    },
    EntityType.ARTIFACT_VERSION: {
        ArtifactVersionState.STAGING: {
            ArtifactVersionState.SCANNING,
            ArtifactVersionState.REJECTED,
            ArtifactVersionState.FAILED,
        },
        ArtifactVersionState.SCANNING: {
            ArtifactVersionState.READY,
            ArtifactVersionState.REJECTED,
            ArtifactVersionState.FAILED,
        },
        ArtifactVersionState.READY: {ArtifactVersionState.DELETED},
    },
    EntityType.TOOL_GRANT: {
        ToolGrantState.ACTIVE: {
            ToolGrantState.SUSPENDED,
            ToolGrantState.REVOKED,
            ToolGrantState.EXPIRED,
        },
        ToolGrantState.SUSPENDED: {
            ToolGrantState.ACTIVE,
            ToolGrantState.REVOKED,
            ToolGrantState.EXPIRED,
        },
    },
    EntityType.TOOL_INVOCATION: {
        ToolInvocationState.CREATED: {
            ToolInvocationState.AUTHORIZED,
            ToolInvocationState.REJECTED,
            ToolInvocationState.CANCELLED,
        },
        ToolInvocationState.AUTHORIZED: {
            ToolInvocationState.EXECUTING,
            ToolInvocationState.REJECTED,
            ToolInvocationState.CANCELLED,
        },
        ToolInvocationState.EXECUTING: {
            ToolInvocationState.SUCCEEDED,
            ToolInvocationState.FAILED,
            ToolInvocationState.UNKNOWN,
            ToolInvocationState.CANCELLED,
        },
        ToolInvocationState.UNKNOWN: {ToolInvocationState.SUCCEEDED, ToolInvocationState.FAILED},
    },
    EntityType.ACTION_PROPOSAL: {
        ActionProposalState.OPEN: {
            ActionProposalState.CONSUMED,
            ActionProposalState.REJECTED,
            ActionProposalState.EXPIRED,
            ActionProposalState.SUPERSEDED,
            ActionProposalState.CANCELLED,
        },
    },
    EntityType.APPROVAL: {
        ApprovalState.PENDING: {
            ApprovalState.APPROVED,
            ApprovalState.REJECTED,
            ApprovalState.EXPIRED,
            ApprovalState.CANCELLED,
        },
    },
    EntityType.EFFECT_LEDGER: {
        EffectState.PREPARED: {EffectState.EXECUTING, EffectState.FAILED},
        EffectState.EXECUTING: {EffectState.SUCCEEDED, EffectState.FAILED, EffectState.UNKNOWN},
        EffectState.UNKNOWN: {EffectState.SUCCEEDED, EffectState.FAILED},
    },
}


def transition(entity: EntityType, current: Enum, target: Enum, command: object | None) -> Enum:
    if not isinstance(entity, EntityType):
        raise InvalidTransitionError(f"unknown entity type: {entity!r}")
    expected_state_type = STATE_TYPES[entity]
    if not isinstance(current, expected_state_type) or not isinstance(target, expected_state_type):
        raise InvalidTransitionError(
            f"{entity.value} transitions require {expected_state_type.__name__} values"
        )
    if target not in TRANSITIONS.get(entity, {}).get(current, set()):
        raise InvalidTransitionError(
            f"illegal {entity.value} transition: {current.value} -> {target.value}"
        )
    if (
        entity is EntityType.RUN
        and current is RunState.NEEDS_ATTENTION
        and target is RunState.CANCELLED
        and not isinstance(command, CompleteCancellation)
    ):
        raise InvalidTransitionError("NEEDS_ATTENTION -> CANCELLED requires CompleteCancellation")
    return target
