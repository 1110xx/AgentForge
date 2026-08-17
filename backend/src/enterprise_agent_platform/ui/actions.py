"""Surface-bound action handler.

A user action carries only the run/surface/revision/action_ref plus an
idempotency key and the displayed digest. The handler resolves the approval
from the immutable Surface revision (never from the client), re-verifies the
revision contract, and forwards the decision to ApprovalDecisionService which
re-checks scope, digest, state, version and expiry inside one transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from enterprise_agent_platform.contracts.commands import UiActionCommand
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.persistence.protocol import PlatformError
from enterprise_agent_platform.ui.service import SurfaceService

Decision = Literal["APPROVE", "REJECT"]

_ACTION_PREFIX = "approval:"


def _parse_action(action_ref: str) -> tuple[str, Decision]:
    if not action_ref.startswith(_ACTION_PREFIX):
        raise PlatformError(
            "ACTION_REF_INVALID", "action ref is not a bound approval action"
        )
    decision_part = action_ref.rsplit(":", 1)[-1]
    if decision_part == "approve":
        decision: Decision = "APPROVE"
    elif decision_part == "reject":
        decision = "REJECT"
    else:
        raise PlatformError(
            "ACTION_REF_INVALID", "action ref does not encode a valid decision"
        )
    approval_id = action_ref[len(_ACTION_PREFIX) : -(len(decision_part) + 1)]
    if not approval_id:
        raise PlatformError("ACTION_REF_INVALID", "action ref has no approval id")
    return approval_id, decision


@dataclass(frozen=True, slots=True)
class SurfaceBoundActionHandler:
    surfaces: SurfaceService
    approvals: object

    async def handle(
        self,
        context: RequestContext,
        command: UiActionCommand,
        *,
        idempotency_key: str,
    ) -> None:
        approval_id, decision = _parse_action(command.action_ref)
        revision = await self.surfaces.revision_contract(
            context.tenant_id,
            command.surface_id,
            command.surface_revision,
        )
        if revision.run_id != command.run_id:
            raise PlatformError(
                "SURFACE_MISMATCH", "action surface revision is not bound to the run"
            )
        props = revision.document.get("props")
        if not isinstance(props, dict):
            raise PlatformError("SURFACE_INVALID", "action surface props are invalid")
        if props.get("approval_id") != approval_id:
            raise PlatformError(
                "SURFACE_MISMATCH", "action surface does not bind this approval"
            )
        expected_key = props.get(
            "approve_key" if decision == "APPROVE" else "reject_key"
        )
        if expected_key != command.action_ref:
            raise PlatformError(
                "ACTION_REF_INVALID", "action ref does not match the surface action"
            )
        await self.approvals.decide(
            context,
            approval_id=approval_id,
            decision=decision,
            displayed_digest=command.displayed_digest or "",
            client_action_id=command.client_action_id,
            idempotency_key=idempotency_key,
        )
