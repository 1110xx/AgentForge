"""Deterministic incident-review closure over the platform's shared services.

This module composes the same durable building blocks the reference vertical
(``reference.provider.ReferenceWorkflowHarness``) uses — ControlPlaneService,
SurfaceService, ApprovalDecisionService/SurfaceBoundActionHandler,
DurableEffectExecutor and the terminal adapter — but for the
``it-incident-investigate`` business scenario (SDD-it-incident-investigate):

  1. a Run investigates an ``incident://`` ticket by reading its ticket / log /
     metric payloads through the IncidentResources content renderer (no LLM);
  2. the investigation publishes an incident report artifact and pauses at an
     ApprovalCard surface bound to an ActionProposal (EXTERNAL_WRITE effect);
  3. a reviewer decides through the same surface-bound action path the public
     ``POST /v1/runs/{id}/actions`` route uses (UiActionCommand + displayed
     digest verification inside one transaction);
  4. APPROVE durably prepares + executes the Effect (the platform reference
     connector simulates the external ITSM write), resumes the successor
     Attempt from the approval Checkpoint and finishes the Run SUCCEEDED;
  5. REJECT consumes the proposal and RECOVERs the Run; a second investigation
     round resumes from the same Checkpoint, produces a revised report artifact
     and re-pauses for approval (SDD §3.4.4 rerun semantics — the human's
     revision guidance reaches the second round through the run's checkpoint /
     followup context rather than a free-text decision field, which the
     platform's durable UiActionCommand intentionally does not carry).

This is the seed the public demo (M4/M5) replays over the running cluster.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Literal

from enterprise_agent_platform.artifacts.service import (
    ArtifactService,
    InMemoryArtifactRepository,
)
from enterprise_agent_platform.contracts.commands import CreateRunCommand, UiActionCommand
from enterprise_agent_platform.contracts.enums import (
    ActionProposalState,
    ArtifactVersionState,
    AttemptState,
    EffectState,
    EntityType,
    ExecutionUnitState,
    RunState,
    StepState,
)
from enterprise_agent_platform.control.approvals import ApprovalDecisionService
from enterprise_agent_platform.control.checkpoints import (
    ApprovalPause,
    ArtifactVersionRef,
    CheckpointCommit,
    pause_for_approval,
)
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.domain.action_digest import compute_action_request_digest
from enterprise_agent_platform.domain.fsm import transition
from enterprise_agent_platform.domain.records import (
    ActionProposalRecord,
    ArtifactRecord,
    ArtifactVersionRecord,
    AttemptRecord,
    CheckpointRecord,
    EffectLedgerRecord,
    ExecutionLeaseRecord,
    ExecutionUnitRecord,
    RunRecord,
    StepRecord,
)
from enterprise_agent_platform.persistence import InMemoryPlatformStore
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore
from enterprise_agent_platform.reference.fake_connector import (
    FailureMode,
    FakeDefectConnector,
    FakeDefectRecord,
)
from enterprise_agent_platform.reference.incident import (
    INCIDENT_DEMO_TICKET_ID,
    IncidentResources,
)
from enterprise_agent_platform.reference.provider import (
    REFERENCE_DEFECT_CONNECTOR_NAME,
    REFERENCE_DEFECT_REQUIRED_SCOPES,
    REFERENCE_DEFECT_TARGET,
    REFERENCE_DEFECT_TOOL_NAME,
    REFERENCE_DEFECT_TOOL_SPEC_DIGEST,
    REFERENCE_DEFECT_TOOL_VERSION,
    ActiveReferenceRuntime,
    MutableClock,
    ReferenceCredentialBroker,
    ReferenceEffectPayloadResolver,
    ReferenceEffectVerifier,
    ReferenceObjectStore,
    ReferenceReconciliationAuthorizer,
    ReferenceScanner,
    ReferenceTerminalAdapter,
)
from enterprise_agent_platform.tools.durable_effects import (
    DurableEffectExecutor,
    ResolvedEffectPayload,
)
from enterprise_agent_platform.ui.actions import SurfaceBoundActionHandler
from enterprise_agent_platform.ui.catalog import A2UI_PROTOCOL_VERSION, PUBLIC_CATALOG_ID
from enterprise_agent_platform.ui.records import PublishedSurface
from enterprise_agent_platform.ui.service import (
    ApprovalSurfaceRequest,
    SurfaceCommitRequest,
    SurfaceService,
)
from enterprise_agent_platform.ui.validator import SurfaceValidator

_REPORT_CLASSIFICATION = "INTERNAL"


def _checksum(payload: object) -> str:
    import hashlib

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class IncidentInvestigation:
    """Payloads one Run round read + the derived report it would publish."""

    ticket: dict[str, object]
    logs: dict[str, object]
    metrics: dict[str, object]
    report: dict[str, object]
    report_bytes: bytes
    report_checksum: str
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IncidentPausedRun:
    context: RequestContext
    run: RunRecord
    unit: ExecutionUnitRecord
    checkpoint: CheckpointRecord
    attempt: AttemptRecord
    lease: ExecutionLeaseRecord
    approval: object
    action_proposal: ActionProposalRecord
    investigation: IncidentInvestigation
    artifact: object
    artifact_surface: PublishedSurface
    approval_surface: PublishedSurface
    round_number: int


@dataclass(frozen=True, slots=True)
class IncidentCompletedRun:
    run: RunRecord
    successor_attempt: AttemptRecord
    effect: EffectLedgerRecord
    approval: object
    external_record: FakeDefectRecord


class IncidentReviewHarness:
    """Executable incident-review vertical (tenant + run composition)."""

    tenant_id = "tenant-incident"

    def __init__(
        self,
        *,
        connector_failure_mode: FailureMode = "none",
        store: PlatformStore | None = None,
        idempotency_seed: str = "",
    ) -> None:
        self.clock = MutableClock()
        self._idempotency_seed = idempotency_seed
        self.store = (
            store if store is not None else InMemoryPlatformStore(clock=self.clock)
        )
        self.control = ControlPlaneService(self.store)
        self.resolver = IncidentResources()
        self.object_store = ReferenceObjectStore()
        self.artifact_repository = InMemoryArtifactRepository()
        self.artifacts = ArtifactService(
            self.object_store,
            self.artifact_repository,
            ReferenceScanner(),
        )
        self.surfaces = SurfaceService(
            self.store,
            SurfaceValidator(
                catalog_id=PUBLIC_CATALOG_ID,
                protocol_version=A2UI_PROTOCOL_VERSION,
            ),
        )
        self.approval_decisions = ApprovalDecisionService(self.store)
        self.ui_actions = SurfaceBoundActionHandler(
            surfaces=self.surfaces,
            approvals=self.approval_decisions,
        )
        self.fake_connector = FakeDefectConnector(failure_mode=connector_failure_mode)
        self.effect_payloads = ReferenceEffectPayloadResolver()
        self.effect_executor = DurableEffectExecutor(
            store=self.store,
            verifier=ReferenceEffectVerifier(),
            reconciliation_authorizer=ReferenceReconciliationAuthorizer(),
            payloads=self.effect_payloads,
            broker=ReferenceCredentialBroker(),
            connectors={REFERENCE_DEFECT_CONNECTOR_NAME: self.fake_connector},
        )
        self.terminal = ReferenceTerminalAdapter(self.store)
        self._run_index = 0

    # ── contexts ──────────────────────────────────────────────────────────

    def _context(self) -> RequestContext:
        return RequestContext(
            tenant_id=self.tenant_id,
            actor_id="incident-analyst",
            scopes=(
                "approvals:decide",
                "approvals:request",
                "runs:create",
                "runs:execute",
            ),
            request_id=f"incident-request-{self._run_index}",
            trace_id=f"incident-trace-{self._run_index}",
        )

    @staticmethod
    def _decision_context(paused: IncidentPausedRun, actor_id: str) -> RequestContext:
        return replace(
            paused.context,
            actor_id=actor_id,
            scopes=tuple(sorted(set(paused.context.scopes) | {"approvals:decide"})),
        )

    # ── investigate / publish / pause ─────────────────────────────────────

    async def investigate(
        self,
        ticket_id: str = INCIDENT_DEMO_TICKET_ID,
    ) -> IncidentInvestigation:
        """Read the ticket + logs + metrics through the incident resolver and
        derive the deterministic report an Agent would write (SDD §3.3/§4)."""
        ticket = json.loads(await self.resolver.read_content(f"incident://ticket/{ticket_id}"))
        if not isinstance(ticket, dict) or ticket.get("kind") != "incident_ticket":
            raise PlatformError("INCIDENT_DATA_MISSING", "demo ticket is not readable")
        service = str(ticket["affected_services"][0])
        logs = json.loads(await self.resolver.read_content(f"incident://logs/{service}"))
        metrics = json.loads(await self.resolver.read_content(f"incident://metrics/{service}"))
        report = {
            "schema_version": "incident-report/v1",
            "ticket_id": ticket_id,
            "summary": (
                f"{ticket['severity']} on {service}: {ticket['description']}"
            ),
            "evidence": {
                "logs": [row["message"] for row in logs["logs"]],
                "metrics": [
                    f"{item['metric']} peak "
                    f"{max(point['value'] for point in item['datapoints'])}"
                    for item in metrics["series"]
                ],
            },
            "root_cause_candidate": "db slow query on pay-service order index",
            "recommendation": "add composite index + rate-limit checkout retries",
            "remediation_action": "itsm.handoff",
        }
        report_bytes = json.dumps(report, ensure_ascii=False, indent=2).encode()
        return IncidentInvestigation(
            ticket=ticket,
            logs=logs,
            metrics=metrics,
            report=report,
            report_bytes=report_bytes,
            report_checksum=_checksum(report),
            evidence=tuple(
                f"{service}:db-slow-query"
                for row in logs["logs"]
                if "db slow query" in row["message"]
            ),
        )

    async def start_and_investigate(
        self,
        ticket_id: str = INCIDENT_DEMO_TICKET_ID,
    ) -> tuple[ActiveReferenceRuntime, IncidentInvestigation]:
        self._run_index += 1
        context = self._context()
        run = await self.control.create_run(
            context,
            CreateRunCommand(
                workflow_type="it-incident-investigate",
                intent="Investigate the incident ticket and prepare a remediation report",
                resource_refs=(f"incident://ticket/{ticket_id}",),
                parameters={"focus": "root-cause", "max_log_entries": 500},
                host_context_ref="host-context:incident-demo",
            ),
            idempotency_key=f"incident-create-{self._idempotency_seed}{self._run_index}",
        )
        unit = await self.store.get_primary_unit(self.tenant_id, run.run_id)
        checkpoint = await self.store.get_checkpoint(
            self.tenant_id, unit.current_checkpoint_id or ""
        )
        reservation = await self.control.reserve_attempt(
            context,
            unit.execution_unit_id,
            checkpoint.checkpoint_id,
            unit.version,
            transition_key=f"incident-reserve-{self._idempotency_seed}{self._run_index}",
        )
        lease = await self.control.activate_lease(
            context,
            reservation.attempt.attempt_id,
            reservation.attempt.generation,
            f"incident-runtime:{reservation.attempt.generation}",
            reservation.lease.version,
        )
        active = ActiveReferenceRuntime(
            context=context,
            run=await self.store.get_run(self.tenant_id, run.run_id),
            unit=await self.store.get_execution_unit(self.tenant_id, unit.execution_unit_id),
            checkpoint=checkpoint,
            attempt=await self.store.get_attempt(self.tenant_id, reservation.attempt.attempt_id),
            lease=lease,
        )
        investigation = await self.investigate(ticket_id=ticket_id)
        return active, investigation

    async def pause_at_review(
        self,
        active: ActiveReferenceRuntime,
        investigation: IncidentInvestigation,
        *,
        round_number: int,
    ) -> IncidentPausedRun:
        """Publish the report artifact + review surfaces and pause the Run at
        an ApprovalCard bound to a durable ActionProposal."""
        await self.artifact_repository.set_active_generation(
            self.tenant_id,
            active.unit.execution_unit_id,
            active.attempt.generation,
        )
        artifact_id = f"incident-report-{active.run.run_id}"
        if round_number > 1:
            artifact_id = f"{artifact_id}-r{round_number}"
        artifact = await self.artifacts.publish(
            tenant_id=self.tenant_id,
            run_id=active.run.run_id,
            execution_unit_id=active.unit.execution_unit_id,
            source_attempt_id=active.attempt.attempt_id,
            artifact_id=artifact_id,
            logical_name=f"{artifact_id}.json",
            classification=_REPORT_CLASSIFICATION,
            content=investigation.report_bytes,
            expected_generation=active.attempt.generation,
        )
        if artifact.state != "READY":
            raise PlatformError("ARTIFACT_NOT_READY", "incident report was not ready")
        await self._persist_artifact_fact(active, artifact)
        artifact_surface = await self.surfaces.commit_revision(
            SurfaceCommitRequest(
                tenant_id=self.tenant_id,
                run_id=active.run.run_id,
                surface_id=f"artifact-{active.run.run_id}-r{round_number}",
                source_attempt_id=active.attempt.attempt_id,
                source_generation=active.attempt.generation,
                catalog_id=PUBLIC_CATALOG_ID,
                protocol_version=A2UI_PROTOCOL_VERSION,
                document={
                    "component": "ArtifactCard",
                    "props": {
                        "title": f"Incident report (round {round_number})",
                        "artifact_id": artifact_id,
                        "version": artifact.version,
                        "download_action_ref": f"artifact:{artifact_id}:download",
                    },
                },
                trace_id=active.context.trace_id,
            )
        )
        step_id = f"step-incident-{active.run.run_id}-r{round_number}"
        proposal = await self._prepare_proposal(
            active,
            investigation=investigation,
            artifact_id=artifact_id,
            step_id=step_id,
            round_number=round_number,
        )
        result = await pause_for_approval(
            self.store,
            active.context,
            attempt_id=active.attempt.attempt_id,
            generation=active.attempt.generation,
            lease_owner=active.lease.owner or "",
            expected_lease_version=active.lease.version,
            checkpoint=CheckpointCommit(
                source_checkpoint_id=active.checkpoint.checkpoint_id,
                workflow_cursor={
                    "node": "await-incident-review",
                    "report_artifact_id": artifact_id,
                    "round": round_number,
                },
                completed_step_ids=(
                    "read-incident-ticket",
                    "read-incident-logs",
                    "read-incident-metrics",
                    "compose-incident-report",
                ),
                active_step_context={
                    "step_id": step_id,
                    "phase": "human-review",
                },
                output_artifact_versions=(
                    ArtifactVersionRef(
                        artifact_id=artifact_id,
                        version=artifact.version,
                    ),
                ),
                resolved_tool_call_ids=(),
                checksum=investigation.report_checksum,
            ),
            approval=ApprovalPause(
                step_id=step_id,
                action_ref=proposal.action_ref,
                approval_type="EXTERNAL_WRITE",
                request_digest=proposal.request_digest,
                canonical_request_ref=proposal.payload_ref,
                expires_at=self.clock() + timedelta(hours=1),
            ),
        )
        approval_surface = await self.surfaces.commit_approval_surface(
            ApprovalSurfaceRequest(
                tenant_id=self.tenant_id,
                run_id=active.run.run_id,
                surface_id=f"approval-{active.run.run_id}-r{round_number}",
                approval_id=result.approval.approval_id,
                title="Execute incident remediation handoff?",
                trace_id=active.context.trace_id,
            )
        )
        return IncidentPausedRun(
            context=active.context,
            run=await self.store.get_run(self.tenant_id, active.run.run_id),
            unit=await self.store.get_execution_unit(self.tenant_id, active.unit.execution_unit_id),
            checkpoint=result.checkpoint,
            attempt=await self.store.get_attempt(self.tenant_id, active.attempt.attempt_id),
            lease=await self.store.get_lease_for_attempt(self.tenant_id, active.attempt.attempt_id),
            approval=result.approval,
            action_proposal=proposal,
            investigation=investigation,
            artifact=artifact,
            artifact_surface=artifact_surface,
            approval_surface=approval_surface,
            round_number=round_number,
        )

    async def _persist_artifact_fact(
        self,
        active: ActiveReferenceRuntime,
        artifact: object,
    ) -> None:
        async with self.store.transaction() as tx:
            now = await tx.db_now()
            await tx.insert_artifact(
                ArtifactRecord(
                    tenant_id=self.tenant_id,
                    artifact_id=artifact.artifact_id,
                    run_id=active.run.run_id,
                    logical_name=artifact.logical_name,
                    artifact_type="incident-report",
                    classification=artifact.classification,
                    retention_policy={"days": 30},
                    state="ACTIVE",
                    current_version=artifact.version,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            await tx.insert_artifact_version(
                ArtifactVersionRecord(
                    tenant_id=self.tenant_id,
                    artifact_id=artifact.artifact_id,
                    version=artifact.version,
                    run_id=active.run.run_id,
                    source_attempt_id=active.attempt.attempt_id,
                    generation=active.attempt.generation,
                    state=ArtifactVersionState.READY,
                    state_version=1,
                    object_uri=artifact.object_key,
                    checksum=artifact.checksum,
                    size_bytes=artifact.size_bytes,
                    media_type="application/json",
                    lineage={"source": f"incident-dataset:{artifact.artifact_id}"},
                    created_at=now,
                    ready_at=now,
                )
            )

    async def _prepare_proposal(
        self,
        active: ActiveReferenceRuntime,
        *,
        investigation: IncidentInvestigation,
        artifact_id: str,
        step_id: str,
        round_number: int,
    ) -> ActionProposalRecord:
        action_ref = f"action:{active.run.run_id}:itsm-handoff-r{round_number}"
        payload_ref = f"restricted:incident-proposal:{active.run.run_id}:r{round_number}"
        canonical_payload = {
            "incident_id": investigation.ticket["ticket_id"],
            "service": investigation.ticket["affected_services"][0],
            "root_cause": investigation.report["root_cause_candidate"],
            "remediation": investigation.report["recommendation"],
            "report_ref": f"artifact:{artifact_id}:1",
            "round": round_number,
        }
        payload_digest = _checksum(canonical_payload)
        request_digest = compute_action_request_digest(
            action_ref=action_ref,
            tool_name=REFERENCE_DEFECT_TOOL_NAME,
            tool_spec_version=REFERENCE_DEFECT_TOOL_VERSION,
            tool_spec_digest=REFERENCE_DEFECT_TOOL_SPEC_DIGEST,
            connector_name=REFERENCE_DEFECT_CONNECTOR_NAME,
            required_scopes=REFERENCE_DEFECT_REQUIRED_SCOPES,
            canonical_target=REFERENCE_DEFECT_TARGET,
            canonical_payload_digest=payload_digest,
            risk_class="WRITE",
        )
        self.effect_payloads.bind(
            tenant_id=self.tenant_id,
            payload_ref=payload_ref,
            payload=ResolvedEffectPayload(arguments=canonical_payload),
        )
        async with self.store.transaction() as tx:
            now = await tx.db_now()
            attempt = await tx.get_attempt(self.tenant_id, active.attempt.attempt_id)
            proposal = ActionProposalRecord(
                tenant_id=self.tenant_id,
                action_ref=action_ref,
                run_id=active.run.run_id,
                step_id=step_id,
                attempt_id=attempt.attempt_id,
                execution_unit_id=active.unit.execution_unit_id,
                source_generation=attempt.generation,
                tool_name=REFERENCE_DEFECT_TOOL_NAME,
                tool_spec_version=REFERENCE_DEFECT_TOOL_VERSION,
                tool_spec_digest=REFERENCE_DEFECT_TOOL_SPEC_DIGEST,
                connector_name=REFERENCE_DEFECT_CONNECTOR_NAME,
                required_scopes=REFERENCE_DEFECT_REQUIRED_SCOPES,
                request_digest=request_digest,
                canonical_payload_digest=payload_digest,
                canonical_target=REFERENCE_DEFECT_TARGET,
                risk_class="WRITE",
                status=ActionProposalState.OPEN,
                version=1,
                payload_ref=payload_ref,
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
            await tx.insert_step(
                StepRecord(
                    tenant_id=self.tenant_id,
                    step_id=step_id,
                    run_id=active.run.run_id,
                    ordinal=round_number,
                    name="Submit incident remediation",
                    step_type="incident.remediation-handoff",
                    policy_snapshot={"write_requires_approval": True},
                    status=StepState.ACTIVE,
                    status_reason=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    ended_at=None,
                )
            )
            await tx.insert_action_proposal(proposal)
            transition(EntityType.ATTEMPT, attempt.status, AttemptState.RUNNING, self)
            running = replace(
                attempt,
                step_id=step_id,
                status=AttemptState.RUNNING,
                version=attempt.version + 1,
                updated_at=now,
            )
            await tx.replace_attempt_cas(running, attempt.version)
            transition(
                EntityType.ATTEMPT,
                running.status,
                AttemptState.CHECKPOINTING,
                self,
            )
            await tx.replace_attempt_cas(
                replace(
                    running,
                    status=AttemptState.CHECKPOINTING,
                    version=running.version + 1,
                    updated_at=now,
                ),
                running.version,
            )
            return proposal

    # ── reviewer decisions ────────────────────────────────────────────────

    def _decision_command(
        self,
        paused: IncidentPausedRun,
        *,
        decision: Literal["APPROVE", "REJECT"],
        client_action_id: str,
    ) -> UiActionCommand:
        suffix = "approve" if decision == "APPROVE" else "reject"
        return UiActionCommand(
            run_id=paused.run.run_id,
            surface_id=paused.approval_surface.surface_id,
            surface_revision=paused.approval_surface.revision.revision,
            action_ref=f"approval:{paused.approval.approval_id}:{suffix}",
            client_action_id=client_action_id,
            displayed_digest=paused.approval.request_digest,
            host_context_ref="host-context:incident-demo",
        )

    async def approve_and_complete(
        self,
        paused: IncidentPausedRun,
        *,
        reviewer: str,
        client_action_id: str,
    ) -> IncidentCompletedRun:
        """Surface-bound APPROVE → durable Effect executes (simulated ITSM
        write) → successor Attempt finishes the Run SUCCEEDED."""
        effect = await self._decide(paused, reviewer, client_action_id, "APPROVE")
        if effect is None or effect.state is not EffectState.SUCCEEDED:
            raise PlatformError("EFFECT_NOT_SUCCEEDED", "incident Effect did not succeed")
        successor = await self._claim_successor(paused, effect)
        successor_attempt = await self.terminal.finish_success(
            paused=paused,
            successor=successor,
            effect=effect,
        )
        external_record = next(
            (
                record
                for record in self.fake_connector.records
                if record.effect_key == effect.effect_key
            ),
            None,
        )
        if external_record is None:
            raise PlatformError(
                "EXTERNAL_RECORD_NOT_FOUND",
                "incident connector has no record for the Effect key",
            )
        return IncidentCompletedRun(
            run=await self.store.get_run(self.tenant_id, paused.run.run_id),
            successor_attempt=successor_attempt,
            effect=await self.store.get_effect(self.tenant_id, effect.effect_id),
            approval=await self.store.get_approval_request(
                self.tenant_id, paused.approval.approval_id
            ),
            external_record=external_record,
        )

    async def _decide(
        self,
        paused: IncidentPausedRun,
        reviewer: str,
        client_action_id: str,
        decision: Literal["APPROVE", "REJECT"],
    ) -> EffectLedgerRecord | None:
        command = self._decision_command(
            paused, decision=decision, client_action_id=client_action_id
        )
        context = self._decision_context(paused, reviewer)
        await self.ui_actions.handle(context, command, idempotency_key=client_action_id)
        if decision == "REJECT":
            return None
        effects = await self.store.list_effects(self.tenant_id, paused.run.run_id)
        if len(effects) != 1:
            raise PlatformError(
                "INTEGRITY_VIOLATION", "approval did not prepare exactly one Effect"
            )
        prepared = effects[0]
        token = f"reference-effect:{self.tenant_id}:{prepared.effect_id}"
        return await self.effect_executor.execute(
            self.tenant_id,
            prepared.effect_id,
            token,
            executor_id="incident-effect-worker",
        )

    async def _claim_successor(
        self,
        paused: IncidentPausedRun,
        effect: EffectLedgerRecord,
    ) -> ActiveReferenceRuntime:
        """Resume the successor Attempt after a succeeded Effect so the
        terminal adapter can finish the Run from the approval Checkpoint."""
        unit = await self.store.get_execution_unit(
            self.tenant_id, paused.unit.execution_unit_id
        )
        run = await self.store.get_run(self.tenant_id, paused.run.run_id)
        if (
            run.status is not RunState.RECOVERING
            or unit.status is not ExecutionUnitState.RECOVERING
            or unit.current_checkpoint_id != paused.checkpoint.checkpoint_id
        ):
            raise PlatformError(
                "INVALID_STATE", "succeeded Effect did not resume checkpoint work"
            )
        reservation = await self.control.reserve_attempt(
            paused.context,
            unit.execution_unit_id,
            paused.checkpoint.checkpoint_id,
            unit.version,
            transition_key=(
                f"incident-effect-resume:{self._idempotency_seed}{effect.effect_id}"
            ),
        )
        lease = await self.control.activate_lease(
            paused.context,
            reservation.attempt.attempt_id,
            reservation.attempt.generation,
            f"incident-runtime:{reservation.attempt.generation}",
            reservation.lease.version,
        )
        return ActiveReferenceRuntime(
            context=paused.context,
            run=await self.store.get_run(self.tenant_id, run.run_id),
            unit=await self.store.get_execution_unit(
                self.tenant_id, unit.execution_unit_id
            ),
            checkpoint=paused.checkpoint,
            attempt=await self.store.get_attempt(
                self.tenant_id, reservation.attempt.attempt_id
            ),
            lease=lease,
        )

    async def reject(
        self,
        paused: IncidentPausedRun,
        *,
        reviewer: str,
        client_action_id: str,
    ) -> RunRecord:
        """Surface-bound REJECT → proposal consumed, Run RECOVERING (the next
        round resumes from the same approval Checkpoint)."""
        await self._decide(paused, reviewer, client_action_id, "REJECT")
        run = await self.store.get_run(self.tenant_id, paused.run.run_id)
        if run.status is not RunState.RECOVERING:
            raise PlatformError("INVALID_STATE", "REJECT did not recover the Run")
        proposal = await self.store.get_action_proposal(
            self.tenant_id, paused.action_proposal.action_ref
        )
        if proposal.status is not ActionProposalState.REJECTED:
            raise PlatformError(
                "INVALID_STATE", "REJECT did not consume the ActionProposal"
            )
        return run

    async def resume_round(
        self,
        paused: IncidentPausedRun,
    ) -> tuple[ActiveReferenceRuntime, IncidentInvestigation]:
        """Resume the Run from the paused approval Checkpoint and re-read the
        investigation for round 2 (SDD rerun semantics: fresh Attempt, same
        Checkpoint cursor). The revised report embeds the reviewer's guidance
        that arrived between rounds (comment → followup context)."""
        unit = await self.store.get_execution_unit(
            self.tenant_id, paused.unit.execution_unit_id
        )
        run = await self.store.get_run(self.tenant_id, paused.run.run_id)
        reservation = await self.control.reserve_attempt(
            paused.context,
            unit.execution_unit_id,
            paused.checkpoint.checkpoint_id,
            unit.version,
            transition_key=f"incident-round2-{self._idempotency_seed}{paused.run.run_id}",
        )
        lease = await self.control.activate_lease(
            paused.context,
            reservation.attempt.attempt_id,
            reservation.attempt.generation,
            f"incident-runtime:{reservation.attempt.generation}",
            reservation.lease.version,
        )
        active = ActiveReferenceRuntime(
            context=paused.context,
            run=run,
            unit=await self.store.get_execution_unit(self.tenant_id, unit.execution_unit_id),
            checkpoint=paused.checkpoint,
            attempt=await self.store.get_attempt(self.tenant_id, reservation.attempt.attempt_id),
            lease=lease,
        )
        investigation = await self.investigate()
        revision_note = (
            "reviewer guidance applied: verify replica lag before scaling"
        )
        revised_report = {
            **investigation.report,
            "revision": 2,
            "revision_note": revision_note,
        }
        return active, IncidentInvestigation(
            ticket=investigation.ticket,
            logs=investigation.logs,
            metrics=investigation.metrics,
            report=revised_report,
            report_bytes=json.dumps(
                revised_report, ensure_ascii=False, indent=2
            ).encode(),
            report_checksum=_checksum(revised_report),
            evidence=investigation.evidence,
        )
