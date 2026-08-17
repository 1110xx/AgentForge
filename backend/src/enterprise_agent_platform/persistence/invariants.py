"""Adapter-independent invariants for approved external actions and Effects."""
from __future__ import annotations

from enterprise_agent_platform.contracts.enums import (
    ActionProposalState,
    ApprovalState,
    EffectState,
)
from enterprise_agent_platform.domain.action_digest import (
    compute_action_request_digest,
    compute_effect_key,
)
from enterprise_agent_platform.domain.records import (
    ActionProposalRecord,
    ApprovalRequestRecord,
    EffectLedgerRecord,
    OutboxMessageRecord,
)

from .protocol import PlatformError


def validate_new_action_proposal(record: ActionProposalRecord) -> None:
    try:
        expected_digest = compute_action_request_digest(
            action_ref=record.action_ref,
            tool_name=record.tool_name,
            tool_spec_version=record.tool_spec_version,
            tool_spec_digest=record.tool_spec_digest,
            connector_name=record.connector_name,
            required_scopes=record.required_scopes,
            canonical_target=record.canonical_target,
            canonical_payload_digest=record.canonical_payload_digest,
            risk_class=record.risk_class,
        )
    except ValueError as error:
        raise PlatformError(
            "INTEGRITY_VIOLATION", "action proposal authority facts are invalid"
        ) from error
    if (
        record.status is not ActionProposalState.OPEN
        or record.version != 1
        or record.request_digest != expected_digest
        or record.expires_at <= record.created_at
    ):
        raise PlatformError(
            "INTEGRITY_VIOLATION",
            "action proposal is not a canonical OPEN authority snapshot",
        )


def validate_new_effect(
    record: EffectLedgerRecord,
    proposal: ActionProposalRecord,
    approval: ApprovalRequestRecord,
) -> None:
    try:
        expected_effect_key = compute_effect_key(
            tenant_id=record.tenant_id,
            run_id=record.run_id,
            action_ref=record.action_ref,
            request_digest=record.request_digest,
        )
    except ValueError as error:
        raise PlatformError("INTEGRITY_VIOLATION", "Effect identity facts are invalid") from error
    if (
        record.state is not EffectState.PREPARED
        or record.version != 1
        or record.executor_id is not None
        or record.execution_epoch != 0
        or record.executor_lease_expires_at is not None
        or record.result_ref is not None
        or record.remote_operation_id is not None
        or record.completed_at is not None
        or record.effect_key != expected_effect_key
        or approval.status is not ApprovalState.APPROVED
        or proposal.status is not ActionProposalState.CONSUMED
        or record.run_id != proposal.run_id
        or record.run_id != approval.run_id
        or record.action_ref != proposal.action_ref
        or record.action_ref != approval.action_ref
        or record.approval_id != approval.approval_id
        or record.request_digest != proposal.request_digest
        or record.request_digest != approval.request_digest
        or record.tool_name != proposal.tool_name
        or record.tool_version != proposal.tool_spec_version
        or record.tool_spec_digest != proposal.tool_spec_digest
        or record.connector_name != proposal.connector_name
        or record.required_scopes != proposal.required_scopes
        or record.canonical_target != proposal.canonical_target
        or record.canonical_payload_digest != proposal.canonical_payload_digest
    ):
        raise PlatformError(
            "INTEGRITY_VIOLATION",
            "Effect is not the exact immutable execution snapshot approved by the user",
        )


def validate_new_outbox(record: OutboxMessageRecord) -> None:
    if (
        record.publish_state != "PENDING"
        or record.version != 1
        or record.delivery_attempts != 0
        or record.next_attempt_at is not None
        or record.last_error is not None
        or record.last_error_code is not None
        or record.published_at is not None
    ):
        raise PlatformError(
            "INTEGRITY_VIOLATION",
            "new Outbox message must be pristine PENDING state at version 1",
        )


def validate_effect_update(
    current: EffectLedgerRecord,
    candidate: EffectLedgerRecord,
) -> None:
    """Keep executor ownership stable across the one-dispatch Effect lifecycle."""
    if current.state is EffectState.PREPARED:
        valid = (
            candidate.state is EffectState.EXECUTING
            and bool(candidate.executor_id)
            and candidate.execution_epoch == current.execution_epoch + 1
            and candidate.executor_lease_expires_at is not None
            and candidate.executor_lease_expires_at > candidate.updated_at
            and candidate.completed_at is None
        )
    elif current.state is EffectState.EXECUTING:
        valid = (
            candidate.state
            in (
                EffectState.UNKNOWN,
                EffectState.SUCCEEDED,
                EffectState.FAILED,
            )
            and candidate.executor_id == current.executor_id
            and candidate.execution_epoch == current.execution_epoch
            and candidate.executor_lease_expires_at is None
            and (
                candidate.completed_at is None
                if candidate.state is EffectState.UNKNOWN
                else candidate.completed_at is not None
            )
        )
    elif current.state is EffectState.UNKNOWN:
        valid = (
            candidate.state in (EffectState.SUCCEEDED, EffectState.FAILED)
            and candidate.executor_id == current.executor_id
            and candidate.execution_epoch == current.execution_epoch
            and candidate.executor_lease_expires_at is None
            and candidate.completed_at is not None
        )
    else:
        valid = False
    if not valid:
        raise PlatformError(
            "INTEGRITY_VIOLATION",
            "Effect state or executor ownership transition is invalid",
        )


__all__ = [
    "validate_effect_update",
    "validate_new_action_proposal",
    "validate_new_effect",
    "validate_new_outbox",
]
