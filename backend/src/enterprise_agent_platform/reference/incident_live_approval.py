"""Live incident approval bridge (M6-C): propose plan + Effect closure.

The live attempt-pod world reuses the platform's durable approval machinery
(approvals.decide / pause_for_approval / ui surfaces / durable effects) with
one structural difference: live runs have no Step records (M6-C decision D6),
so the gate and the decision are run-level (``approval.step_id=None``).

This module owns everything incident-specific so the platform core stays
vertical-agnostic:

* ``normalize_incident_handoff_proposal`` — canonicalises a child ``propose``
  whose ``action_ref`` targets the registered ITSM remediation handoff so the
  resulting OPEN proposal carries the reference defect connector constants the
  durable Effect executor can run (defect.create on ``project:reference``);
* ``IncidentEffectPayloadResolver`` — deterministic Effect arguments resolved
  from durable Run facts (ticket id from the intent + the latest READY report
  artifact), no in-memory bindings to lose across restarts;
* ``IncidentApprovalUiClosure`` — after an APPROVE decision, executes the
  prepared Effect through the durable executor and closes the Run SUCCEEDED
  (step-less terminal), idempotently.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Literal

from enterprise_agent_platform.contracts.enums import ApprovalState, EffectState, RunState
from enterprise_agent_platform.control.approval_closure import close_run_after_effect
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore
from enterprise_agent_platform.reference.fake_connector import FakeDefectConnector
from enterprise_agent_platform.reference.provider import (
    REFERENCE_DEFECT_CONNECTOR_NAME,
    REFERENCE_DEFECT_REQUIRED_SCOPES,
    REFERENCE_DEFECT_TARGET,
    REFERENCE_DEFECT_TOOL_NAME,
    REFERENCE_DEFECT_TOOL_SPEC_DIGEST,
    REFERENCE_DEFECT_TOOL_VERSION,
    ReferenceCredentialBroker,
    ReferenceEffectVerifier,
    ReferenceReconciliationAuthorizer,
)
from enterprise_agent_platform.tools.durable_effects import (
    DurableEffectExecutor,
    ResolvedEffectPayload,
)

HANDOFF_ACTION_REF = "itsm.handoff"
HANDOFF_RISK_CLASS = "WRITE"
_PAYLOAD_PREFIX = "restricted:incident-proposal:"
_TICKET_PATTERN = re.compile(r"\b(T\d{6,})\b")

REJECT_DEFAULT_REVISION_NOTE = (
    "评审驳回：请依据评审意见修订报告（补充关键证据、明确根因与修复建议），"
    "重新发布报告后再次提交审批。"
)


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _ticket_id_from_intent(intent: str) -> str:
    match = _TICKET_PATTERN.search(intent or "")
    return match.group(1) if match else ""


def _handoff_arguments(
    *,
    tenant_id: str,
    run_id: str,
    intent: str,
    ready_artifacts: tuple[Any, ...],
) -> dict[str, str]:
    report_ref = ""
    for artifact in ready_artifacts:
        report_ref = f"{artifact.artifact_id}@{artifact.version}"
    return {
        "incident_id": _ticket_id_from_intent(intent),
        "run_id": run_id,
        "report_ref": report_ref,
        "tenant_id": tenant_id,
    }


def proposal_is_incident_handoff(payload_ref: str) -> bool:
    return str(payload_ref or "").startswith(_PAYLOAD_PREFIX)


@dataclass(frozen=True, slots=True)
class IncidentHandoffNormalization:
    action_ref: str
    tool_name: str
    tool_spec_version: str
    tool_spec_digest: str
    connector_name: str
    required_scopes: tuple[str, ...]
    canonical_target: str
    canonical_payload_digest: str
    payload_ref: str
    risk_class: str


def normalize_incident_handoff(
    *,
    action_ref: str,
    tenant_id: str,
    run_id: str,
    generation: int,
    intent: str,
    ready_artifacts: tuple[Any, ...],
) -> IncidentHandoffNormalization | None:
    """Return canonical proposal fields for the registered ITSM handoff plan.

    Only ``action_ref == HANDOFF_ACTION_REF`` proposals are canonicalised; any
    other proposal keeps the caller's default (record-only OPEN proposal that
    never gates, so the live approval loop stays deterministic).
    """
    if action_ref != HANDOFF_ACTION_REF:
        return None
    arguments = _handoff_arguments(
        tenant_id=tenant_id,
        run_id=run_id,
        intent=intent,
        ready_artifacts=ready_artifacts,
    )
    return IncidentHandoffNormalization(
        action_ref=action_ref,
        tool_name=REFERENCE_DEFECT_TOOL_NAME,
        tool_spec_version=REFERENCE_DEFECT_TOOL_VERSION,
        tool_spec_digest=REFERENCE_DEFECT_TOOL_SPEC_DIGEST,
        connector_name=REFERENCE_DEFECT_CONNECTOR_NAME,
        required_scopes=REFERENCE_DEFECT_REQUIRED_SCOPES,
        canonical_target=REFERENCE_DEFECT_TARGET,
        canonical_payload_digest=_digest(arguments),
        payload_ref=f"{_PAYLOAD_PREFIX}{run_id}:g{generation}",
        risk_class=HANDOFF_RISK_CLASS,
    )


class IncidentEffectPayloadResolver:
    """Durable Effect arguments derived from Run facts (restart-safe)."""

    def __init__(self, store: PlatformStore) -> None:
        self._store = store

    async def resolve(
        self, *, tenant_id: str, payload_ref: str
    ) -> ResolvedEffectPayload:
        if not payload_ref.startswith(_PAYLOAD_PREFIX):
            raise PlatformError(
                "ACTION_PAYLOAD_NOT_FOUND", "incident payload reference is unknown"
            )
        remainder = payload_ref[len(_PAYLOAD_PREFIX) :]
        run_id = remainder.split(":")[0]
        run = await self._store.get_run(tenant_id, run_id)
        ready = await self._store.list_ready_artifacts_for_run(tenant_id, run_id)
        return ResolvedEffectPayload(
            arguments=_handoff_arguments(
                tenant_id=tenant_id,
                run_id=run_id,
                intent=run.intent,
                ready_artifacts=ready,
            )
        )


class IncidentApprovalUiClosure:
    """Post-APPROVE bridge: execute the durable Effect, then close the Run.

    REJECT needs no closure work here: ``ApprovalDecisionService.decide``
    already reopened the Run to RECOVERING and queued the reviewer revision
    note as a follow-up (round-2 attempt answers it and re-proposes).
    """

    def __init__(self, store: PlatformStore, connector_failure_mode: str = "none") -> None:
        self._store = store
        self._connectors = {
            REFERENCE_DEFECT_CONNECTOR_NAME: FakeDefectConnector(
                failure_mode=connector_failure_mode  # type: ignore[arg-type]
            )
        }
        self._executor = DurableEffectExecutor(
            store=store,
            verifier=ReferenceEffectVerifier(),
            reconciliation_authorizer=ReferenceReconciliationAuthorizer(),
            payloads=IncidentEffectPayloadResolver(store),
            broker=ReferenceCredentialBroker(),
            connectors=self._connectors,
        )

    async def after_decision(
        self,
        context: RequestContext,
        *,
        approval_id: str,
        decision: Literal["APPROVE", "REJECT"],
        idempotency_key: str,
    ) -> None:
        if decision != "APPROVE":
            return
        approval = await self._store.get_approval_request(
            context.tenant_id, approval_id
        )
        if approval.status is not ApprovalState.APPROVED:
            return
        run = await self._store.get_run(context.tenant_id, approval.run_id)
        if run.status is RunState.SUCCEEDED:
            return
        effects = await self._store.list_effects(
            context.tenant_id, approval.run_id
        )
        prepared = [
            effect
            for effect in effects
            if effect.approval_id == approval_id
            and effect.state is EffectState.PREPARED
        ]
        if not prepared:
            raise PlatformError(
                "EFFECT_NOT_PREPARED",
                "approved approval has no prepared Effect to execute",
            )
        effect = prepared[0]
        executed = await self._executor.execute(
            context.tenant_id,
            effect.effect_id,
            f"reference-effect:{context.tenant_id}:{effect.effect_id}",
            executor_id="live-approval-worker",
        )
        if executed.state is not EffectState.SUCCEEDED:
            raise PlatformError(
                "EFFECT_EXECUTION_FAILED",
                f"approved Effect ended in {executed.state.value}",
            )
        await close_run_after_effect(
            self._store,
            replace(
                context,
                scopes=tuple(sorted(set(context.scopes) | {"runs:execute"})),
            ),
            run_id=approval.run_id,
            effect_id=executed.effect_id,
            transition_key=f"incident-effect-resume:{idempotency_key}",
        )


def install_incident_approval_bridge(container: Any, store: PlatformStore) -> Any:
    """Wire the live incident approval bridge onto an API container (M6-C).

    Adds the ApprovalCard ui-action handler (surface-bound decisions plus the
    approve closure), the orchestrator hooks that canonicalise the ITSM
    handoff proposal (``itsm.handoff``) and pause the Run at final commit, and
    the store-backed SurfaceService both sides share. Pure host composition:
    the platform core stays vertical-agnostic.
    """
    from dataclasses import replace

    from enterprise_agent_platform.control.approvals import ApprovalDecisionService
    from enterprise_agent_platform.ui.actions import SurfaceBoundActionHandler
    from enterprise_agent_platform.ui.catalog import (
        A2UI_PROTOCOL_VERSION,
        PUBLIC_CATALOG_ID,
    )
    from enterprise_agent_platform.ui.service import SurfaceService
    from enterprise_agent_platform.ui.validator import SurfaceValidator

    surfaces = SurfaceService(
        store=store,
        validator=SurfaceValidator(
            catalog_id=PUBLIC_CATALOG_ID,
            protocol_version=A2UI_PROTOCOL_VERSION,
        ),
    )

    async def _plan(
        action_ref,
        *,
        tenant_id,
        run_id,
        generation,
        intent,
        ready_artifacts,
    ):
        return normalize_incident_handoff(
            action_ref=action_ref,
            tenant_id=tenant_id,
            run_id=run_id,
            generation=generation,
            intent=intent,
            ready_artifacts=ready_artifacts,
        )

    def _gate(proposal) -> bool:
        return proposal_is_incident_handoff(proposal.payload_ref)

    actions = SurfaceBoundActionHandler(
        surfaces=surfaces,
        approvals=ApprovalDecisionService(store),
        after_decision=IncidentApprovalUiClosure(store),
    )
    return replace(
        container,
        ui_actions=actions,
        approval_surfaces=surfaces,
        action_planner=_plan,
        approval_gate=_gate,
    )


__all__ = [
    "HANDOFF_ACTION_REF",
    "REJECT_DEFAULT_REVISION_NOTE",
    "IncidentApprovalUiClosure",
    "IncidentEffectPayloadResolver",
    "IncidentHandoffNormalization",
    "install_incident_approval_bridge",
    "normalize_incident_handoff",
    "proposal_is_incident_handoff",
]
