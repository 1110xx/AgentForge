"""Executable process-local vertical assembled only from public platform ports.

The reference harness composes the shared durable approval, Effect, checkpoint and
control-plane services with deterministic adapters. Only the final Runtime
completion command remains a process-local adapter because the public core does not
yet expose that command as a standalone service.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from enterprise_agent_platform.artifacts.service import (
    ArtifactService,
    InMemoryArtifactRepository,
    ScanResult,
)
from enterprise_agent_platform.artifacts.service import (
    ArtifactVersionRecord as PublishedArtifactVersion,
)
from enterprise_agent_platform.contracts.commands import CreateRunCommand, UiActionCommand
from enterprise_agent_platform.contracts.enums import (
    ActionProposalState,
    ArtifactVersionState,
    AttemptState,
    EffectState,
    EntityType,
    EventType,
    ExecutionLeaseState,
    ExecutionUnitState,
    RunState,
    StepState,
    ToolRiskClass,
)
from enterprise_agent_platform.contracts.events import (
    AttemptLifecyclePayload,
    EnterpriseEventEnvelope,
    RunStatusChangedPayload,
)
from enterprise_agent_platform.contracts.models import RunEventPage
from enterprise_agent_platform.control.approvals import ApprovalDecisionService
from enterprise_agent_platform.control.checkpoints import (
    ApprovalPause,
    ArtifactVersionRef,
    CheckpointCommit,
    pause_for_approval,
)
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.reconciler import recover_expired_lease
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.control.views import RunQueryService
from enterprise_agent_platform.domain.action_digest import compute_action_request_digest
from enterprise_agent_platform.domain.fsm import transition
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
    OutboxMessageRecord,
    RecoveryResult,
    RunRecord,
    StepRecord,
)
from enterprise_agent_platform.persistence import InMemoryPlatformStore
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore
from enterprise_agent_platform.reference.adapter import (
    SyntheticAnalysis,
    SyntheticAnalysisAdapter,
    SyntheticReadConnector,
    SyntheticReadResult,
)
from enterprise_agent_platform.reference.dataset import REFERENCE_RESOURCE_REF
from enterprise_agent_platform.reference.fake_connector import (
    FailureMode,
    FakeDefectConnector,
)
from enterprise_agent_platform.security.capabilities import (
    VerifiedEffectCapability,
    VerifiedRuntimeCapability,
)
from enterprise_agent_platform.tools.connectors import CredentialMaterial
from enterprise_agent_platform.tools.durable_effects import (
    DurableEffectExecutor,
    ReconciledDurableEffect,
    ResolvedEffectPayload,
    VerifiedEffectReconciliation,
    compute_reconciliation_evidence_digest,
)
from enterprise_agent_platform.tools.gateway import (
    GatewayAuthorization,
    InMemoryInvocationRepository,
    ToolGateway,
    ToolInvocationRequest,
    ToolInvocationResult,
)
from enterprise_agent_platform.tools.registry import (
    InMemoryToolGrantStore,
    ToolGrant,
    ToolRegistry,
    ToolSpec,
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


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


REFERENCE_DEFECT_TOOL_NAME = "defect.create"
REFERENCE_DEFECT_TOOL_VERSION = "v1"
REFERENCE_DEFECT_CONNECTOR_NAME = "reference-defects"
REFERENCE_DEFECT_REQUIRED_SCOPES = ("defect:write",)
REFERENCE_DEFECT_TARGET = "project:reference"
REFERENCE_DEFECT_TOOL_SPEC_DIGEST = _digest(
    {
        "name": REFERENCE_DEFECT_TOOL_NAME,
        "version": REFERENCE_DEFECT_TOOL_VERSION,
        "connector_name": REFERENCE_DEFECT_CONNECTOR_NAME,
        "operation": "defect.create",
        "risk_class": "WRITE",
        "required_scopes": REFERENCE_DEFECT_REQUIRED_SCOPES,
        "allowed_resource_prefixes": ("project:",),
        "required_input_fields": (
            "evidence_refs",
            "report_checksum",
            "source_checksum",
            "title",
        ),
    }
)


class MutableClock:
    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


class ReferenceObjectStore:
    """Immutable byte store implementing the Artifact ObjectStore port."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    async def put(self, key: str, content: bytes) -> None:
        current = self._objects.get(key)
        if current is not None and current != content:
            raise PlatformError("IMMUTABLE_OBJECT", "reference object cannot be overwritten")
        self._objects[key] = bytes(content)

    async def get(self, key: str) -> bytes:
        try:
            return self._objects[key]
        except KeyError as error:
            raise PlatformError("NOT_FOUND", "reference object does not exist") from error

    async def exists(self, key: str) -> bool:
        return key in self._objects

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)


class ReferenceScanner:
    async def scan(self, content: bytes, *, digest: str) -> ScanResult:
        if digest != f"sha256:{hashlib.sha256(content).hexdigest()}":
            return ScanResult(
                clean=False,
                scanner_version="reference-scanner/v1",
                reason_code="DIGEST",
            )
        return ScanResult(clean=True, scanner_version="reference-scanner/v1")


class ReferenceCredentialBroker:
    async def acquire(
        self, *, tenant_id: str, connector_name: str, resource_ref: str
    ) -> CredentialMaterial:
        del tenant_id, connector_name, resource_ref
        return CredentialMaterial(secret={"reference_key": "reference-only"})


class ReferenceResultStore:
    def __init__(self) -> None:
        self._values: dict[str, bytes] = {}

    async def put(self, digest: str, payload: bytes) -> str:
        reference = f"tool-result:{digest[7:]}"
        current = self._values.get(reference)
        if current is not None and current != payload:
            raise PlatformError("RESULT_DIGEST_COLLISION", "reference result digest collided")
        self._values[reference] = bytes(payload)
        return reference

    async def get(self, reference: str) -> bytes:
        try:
            return self._values[reference]
        except KeyError as error:
            raise PlatformError("NOT_FOUND", "reference tool result does not exist") from error


class ReferenceRuntimeVerifier:
    async def verify_runtime(
        self,
        token: str,
        *,
        tenant_id: str,
        run_id: str,
        attempt_id: str,
        generation: int,
        required_scopes: tuple[str, ...],
    ) -> VerifiedRuntimeCapability:
        del required_scopes
        if token != f"reference-runtime:{attempt_id}:{generation}":
            raise PlatformError("INVALID_CAPABILITY", "reference runtime token is invalid")
        now = datetime.now(UTC)
        return VerifiedRuntimeCapability(
            token_id=f"reference-runtime-token:{attempt_id}",
            issuer="reference-harness",
            audience="tool-gateway",
            tenant_id=tenant_id,
            run_id=run_id,
            execution_unit_id="reference-unit-verified-by-gateway-request",
            attempt_id=attempt_id,
            generation=generation,
            scopes=("synthetic:read",),
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )


@dataclass(frozen=True, slots=True)
class ReadToolExecution:
    invocation: ToolInvocationResult
    result: SyntheticReadResult


class SyntheticReadProvider:
    """Register and invoke the synthetic READ tool through ToolGateway."""

    def __init__(self, adapter: SyntheticAnalysisAdapter) -> None:
        self._adapter = adapter
        self.connector = SyntheticReadConnector(adapter)
        self.results = ReferenceResultStore()
        self.invocations = InMemoryInvocationRepository()

    async def execute(
        self,
        *,
        tenant_id: str,
        run_id: str,
        execution_unit_id: str,
        attempt_id: str,
        generation: int,
    ) -> ReadToolExecution:
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="synthetic.results.read",
                version="v1",
                connector_name="synthetic-reference",
                operation="synthetic.read",
                risk_class=ToolRiskClass.READ,
                required_scopes=("synthetic:read",),
                allowed_resource_prefixes=("synthetic-dataset:",),
                required_input_fields=("max_items",),
                optional_input_fields=("suite",),
                required_output_fields=(
                    "resource_ref",
                    "dataset_version",
                    "case_count",
                    "failed_count",
                    "checksum",
                    "cases",
                ),
                timeout_seconds=2,
                max_result_bytes=64 * 1024,
            )
        )
        grant_id = f"grant:{attempt_id}"
        grants = InMemoryToolGrantStore()
        grants.add(
            ToolGrant(
                grant_id=grant_id,
                tenant_id=tenant_id,
                run_id=run_id,
                attempt_id=attempt_id,
                tool_name="synthetic.results.read",
                tool_version="v1",
                scopes=("synthetic:read",),
                resource_prefixes=("synthetic-dataset:",),
                active=True,
            )
        )
        gateway = ToolGateway(
            registry=registry,
            grants=grants,
            capability_verifier=ReferenceRuntimeVerifier(),
            credential_broker=ReferenceCredentialBroker(),
            connectors={"synthetic-reference": self.connector},
            invocations=self.invocations,
            results=self.results,
            authorization_provider=lambda: GatewayAuthorization(
                principal_scopes=("synthetic:read",),
                run_policy_scopes=("synthetic:read",),
            ),
        )
        invocation = await gateway.invoke(
            f"reference-runtime:{attempt_id}:{generation}",
            ToolInvocationRequest(
                tenant_id=tenant_id,
                run_id=run_id,
                execution_unit_id=execution_unit_id,
                attempt_id=attempt_id,
                generation=generation,
                call_id=f"synthetic-read:{attempt_id}",
                tool_name="synthetic.results.read",
                tool_version="v1",
                grant_id=grant_id,
                resource_ref=REFERENCE_RESOURCE_REF,
                arguments={"max_items": 100},
            ),
        )
        expected = self._adapter.read(REFERENCE_RESOURCE_REF, max_items=100)
        stored = await self.results.get(invocation.result_ref)
        encoded_expected = json.dumps(
            expected.to_connector_output(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if stored != encoded_expected:
            raise PlatformError("INTEGRITY_VIOLATION", "reference READ result changed in transit")
        return ReadToolExecution(invocation=invocation, result=expected)


class ReferenceEffectVerifier:
    async def verify_effect(
        self,
        token: str,
        *,
        tenant_id: str,
        effect_id: str,
        approval_id: str,
        request_digest: str,
        required_scopes: tuple[str, ...],
    ) -> VerifiedEffectCapability:
        if token != f"reference-effect:{tenant_id}:{effect_id}":
            raise PlatformError("INVALID_CAPABILITY", "reference effect token is invalid")
        now = datetime.now(UTC)
        return VerifiedEffectCapability(
            token_id=f"reference-effect-token:{effect_id}",
            issuer="reference-harness",
            audience="effect-executor",
            tenant_id=tenant_id,
            effect_id=effect_id,
            approval_id=approval_id,
            request_digest=request_digest,
            tool_name=REFERENCE_DEFECT_TOOL_NAME,
            tool_version=REFERENCE_DEFECT_TOOL_VERSION,
            tool_spec_digest=REFERENCE_DEFECT_TOOL_SPEC_DIGEST,
            connector_name=REFERENCE_DEFECT_CONNECTOR_NAME,
            canonical_target=REFERENCE_DEFECT_TARGET,
            scopes=required_scopes,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )


class ReferenceEffectPayloadResolver:
    def __init__(self) -> None:
        self._payloads: dict[tuple[str, str], ResolvedEffectPayload] = {}

    def bind(
        self,
        *,
        tenant_id: str,
        payload_ref: str,
        payload: ResolvedEffectPayload,
    ) -> None:
        key = (tenant_id, payload_ref)
        existing = self._payloads.get(key)
        if existing is not None and existing != payload:
            raise PlatformError("IMMUTABLE_ACTION_PAYLOAD", "reference payload binding changed")
        self._payloads[key] = payload

    async def resolve(self, *, tenant_id: str, payload_ref: str) -> ResolvedEffectPayload:
        try:
            payload = self._payloads[(tenant_id, payload_ref)]
        except KeyError as error:
            raise PlatformError(
                "ACTION_PAYLOAD_NOT_FOUND", "reference action payload is unavailable"
            ) from error
        return ResolvedEffectPayload(arguments=dict(payload.arguments))


class ReferenceReconciliationAuthorizer:
    async def verify_reconciliation(
        self,
        token: str,
        *,
        tenant_id: str,
        effect_id: str,
        effect_key: str,
        evidence_digest: str,
    ) -> VerifiedEffectReconciliation:
        expected = (
            f"reference-reconciliation:{tenant_id}:{effect_id}:{effect_key}:{evidence_digest}"
        )
        if token != expected:
            raise PlatformError("INVALID_CAPABILITY", "reference reconciliation token is invalid")
        return VerifiedEffectReconciliation(
            actor_id="service:reference-effect-reconciler",
            executor_inactive=True,
            observation_stable=True,
        )


@dataclass(frozen=True, slots=True)
class ActiveReferenceRuntime:
    context: RequestContext
    run: RunRecord
    unit: ExecutionUnitRecord
    checkpoint: CheckpointRecord
    attempt: AttemptRecord
    lease: ExecutionLeaseRecord


@dataclass(frozen=True, slots=True)
class PausedReferenceRun:
    context: RequestContext
    run: RunRecord
    unit: ExecutionUnitRecord
    checkpoint: CheckpointRecord
    attempt: AttemptRecord
    lease: ExecutionLeaseRecord
    approval: ApprovalRequestRecord
    action_proposal: ActionProposalRecord
    analysis: SyntheticAnalysis
    artifact: PublishedArtifactVersion
    evidence_surface: PublishedSurface
    artifact_surface: PublishedSurface
    approval_surface: PublishedSurface
    disconnect_after_event_seq: int


@dataclass(frozen=True, slots=True)
class CompletedReferenceRun:
    run: RunRecord
    successor_attempt: AttemptRecord
    effect: EffectLedgerRecord
    approval: ApprovalRequestRecord


class ReferenceTerminalAdapter:
    """Process-local implementation of the not-yet-public final Runtime command."""

    def __init__(self, store: InMemoryPlatformStore) -> None:
        self._store = store

    async def finish_success(
        self,
        paused: PausedReferenceRun,
        successor: ActiveReferenceRuntime,
        effect: EffectLedgerRecord,
    ) -> AttemptRecord:
        if effect.state is not EffectState.SUCCEEDED:
            raise PlatformError(
                "EFFECT_NOT_SUCCEEDED", "only a succeeded Effect can finish the Run"
            )
        tenant_id = successor.context.tenant_id
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            run = await tx.lock_run(tenant_id, successor.run.run_id)
            unit = await tx.lock_execution_unit(tenant_id, successor.unit.execution_unit_id)
            attempt = await tx.get_attempt(tenant_id, successor.attempt.attempt_id)
            lease = await tx.get_lease_for_attempt(tenant_id, attempt.attempt_id)
            step = await tx.get_step(tenant_id, paused.approval.step_id or "")
            if (
                run.status is not RunState.RUNNING
                or unit.status is not ExecutionUnitState.EXECUTING
                or step.status is not StepState.ACTIVE
                or attempt.status is not AttemptState.CLAIMED
                or lease.state is not ExecutionLeaseState.ACTIVE
            ):
                raise PlatformError("INVALID_STATE", "successor Runtime is not ready to finish")
            transition(EntityType.ATTEMPT, attempt.status, AttemptState.RUNNING, effect)
            running_attempt = replace(
                attempt,
                status=AttemptState.RUNNING,
                version=attempt.version + 1,
                updated_at=now,
            )
            await tx.replace_attempt_cas(running_attempt, attempt.version)
            transition(
                EntityType.ATTEMPT,
                running_attempt.status,
                AttemptState.SUCCEEDED,
                effect,
            )
            transition(
                EntityType.EXECUTION_LEASE,
                lease.state,
                ExecutionLeaseState.RELEASED,
                effect,
            )
            transition(
                EntityType.EXECUTION_UNIT,
                unit.status,
                ExecutionUnitState.SUCCEEDED,
                effect,
            )
            transition(EntityType.STEP, step.status, StepState.SUCCEEDED, effect)
            transition(EntityType.RUN, run.status, RunState.SUCCEEDED, effect)

            succeeded_attempt = replace(
                running_attempt,
                status=AttemptState.SUCCEEDED,
                version=running_attempt.version + 1,
                updated_at=now,
                ended_at=now,
            )
            released_lease = replace(
                lease,
                state=ExecutionLeaseState.RELEASED,
                version=lease.version + 1,
                released_at=now,
                updated_at=now,
            )
            succeeded_unit = replace(
                unit,
                status=ExecutionUnitState.SUCCEEDED,
                version=unit.version + 1,
                updated_at=now,
            )
            succeeded_step = replace(
                step,
                status=StepState.SUCCEEDED,
                status_reason=None,
                version=step.version + 1,
                updated_at=now,
                ended_at=now,
            )
            attempt_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self._store.new_id("event"),
                tenant_id=tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 1,
                event_type=EventType.ATTEMPT_LIFECYCLE,
                occurred_at=now,
                producer_service="reference-terminal-adapter",
                payload_schema="attempt-lifecycle/v1",
                payload=AttemptLifecyclePayload(
                    kind="attempt.lifecycle",
                    attempt_id=attempt.attempt_id,
                    status=AttemptState.SUCCEEDED,
                ),
                attempt_id=attempt.attempt_id,
                trace_id=successor.context.trace_id,
            )
            run_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self._store.new_id("event"),
                tenant_id=tenant_id,
                run_id=run.run_id,
                event_seq=attempt_event.event_seq + 1,
                event_type=EventType.RUN_STATUS_CHANGED,
                occurred_at=now,
                producer_service="reference-terminal-adapter",
                payload_schema="run-status/v1",
                payload=RunStatusChangedPayload(
                    kind="run.status.changed",
                    previous=run.status,
                    current=RunState.SUCCEEDED,
                ),
                attempt_id=attempt.attempt_id,
                causation_event_id=attempt_event.event_id,
                trace_id=successor.context.trace_id,
            )
            succeeded_run = replace(
                run,
                status=RunState.SUCCEEDED,
                status_reason=None,
                version=run.version + 1,
                last_event_seq=run_event.event_seq,
                updated_at=now,
                ended_at=now,
            )
            await tx.replace_attempt_cas(succeeded_attempt, running_attempt.version)
            await tx.replace_lease_cas(released_lease, lease.version)
            await tx.replace_execution_unit_cas(succeeded_unit, unit.version)
            await tx.replace_step_cas(succeeded_step, step.version)
            await tx.replace_run_cas(succeeded_run, run.version)
            await tx.append_event(attempt_event, run.last_event_seq)
            await tx.append_event(run_event, attempt_event.event_seq)
            await tx.insert_audit(
                AuditEventRecord(
                    tenant_id=tenant_id,
                    audit_event_id=self._store.new_id("audit"),
                    run_id=run.run_id,
                    actor_id="reference-runtime",
                    action="run.succeeded.reference",
                    entity_type="run",
                    entity_id=run.run_id,
                    entity_version=succeeded_run.version,
                    outcome="SUCCEEDED",
                    trace_id=successor.context.trace_id,
                    details={"effect_id": effect.effect_id},
                    created_at=now,
                )
            )
            await tx.insert_outbox(
                OutboxMessageRecord(
                    tenant_id=tenant_id,
                    message_id=self._store.new_id("outbox"),
                    run_id=run.run_id,
                    topic="run.terminal",
                    payload={"run_id": run.run_id},
                    event_id=run_event.event_id,
                    aggregate_version=succeeded_run.version,
                    created_at=now,
                    published_at=None,
                )
            )
            return succeeded_attempt


class ReferenceWorkflowHarness:
    """Reference vertical used by tests and local architecture demonstrations."""

    tenant_id = "tenant-reference"

    def __init__(
        self,
        *,
        connector_failure_mode: FailureMode = "none",
        store: PlatformStore | None = None,
    ) -> None:
        self.clock = MutableClock()
        self.store = (
            store if store is not None else InMemoryPlatformStore(clock=self.clock)
        )
        self.control = ControlPlaneService(self.store)
        self.queries = RunQueryService(self.store)
        self.adapter = SyntheticAnalysisAdapter()
        self.read_provider = SyntheticReadProvider(self.adapter)
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
            connectors={"reference-defects": self.fake_connector},
        )
        self.terminal = ReferenceTerminalAdapter(self.store)
        self._run_index = 0
        self._effect_tokens: dict[str, str] = {}

    def _context(self) -> RequestContext:
        return RequestContext(
            tenant_id=self.tenant_id,
            actor_id="analyst-reference",
            scopes=(
                "approvals:decide",
                "approvals:request",
                "runs:create",
                "runs:execute",
            ),
            request_id=f"reference-request-{self._run_index}",
            trace_id=f"reference-trace-{self._run_index}",
        )

    @staticmethod
    def _decision_context(paused: PausedReferenceRun, actor_id: str) -> RequestContext:
        return replace(
            paused.context,
            actor_id=actor_id,
            scopes=tuple(sorted(set(paused.context.scopes) | {"approvals:decide"})),
        )

    async def start_active_runtime(self) -> ActiveReferenceRuntime:
        self._run_index += 1
        context = self._context()
        run = await self.control.create_run(
            context,
            CreateRunCommand(
                workflow_type="synthetic-analysis",
                intent="Analyze the deterministic synthetic failure dataset",
                resource_refs=(REFERENCE_RESOURCE_REF,),
                parameters={"analysis_mode": "failure-pattern", "max_items": 100},
                host_context_ref="host-context:reference",
            ),
            idempotency_key=f"reference-create-{self._run_index}",
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
            transition_key=f"reference-reserve-{self._run_index}",
        )
        lease = await self.control.activate_lease(
            context,
            reservation.attempt.attempt_id,
            reservation.attempt.generation,
            f"reference-runtime:{reservation.attempt.generation}",
            reservation.lease.version,
        )
        return ActiveReferenceRuntime(
            context=context,
            run=await self.store.get_run(self.tenant_id, run.run_id),
            unit=await self.store.get_execution_unit(self.tenant_id, unit.execution_unit_id),
            checkpoint=checkpoint,
            attempt=await self.store.get_attempt(self.tenant_id, reservation.attempt.attempt_id),
            lease=lease,
        )

    async def run_to_approval(self) -> PausedReferenceRun:
        active = await self.start_active_runtime()
        await self.artifact_repository.set_active_generation(
            self.tenant_id,
            active.unit.execution_unit_id,
            active.attempt.generation,
        )
        read = await self.read_provider.execute(
            tenant_id=self.tenant_id,
            run_id=active.run.run_id,
            execution_unit_id=active.unit.execution_unit_id,
            attempt_id=active.attempt.attempt_id,
            generation=active.attempt.generation,
        )
        analysis = self.adapter.analyze(read.result)
        artifact_id = f"analysis-report-{active.run.run_id}"
        artifact = await self.artifacts.publish(
            tenant_id=self.tenant_id,
            run_id=active.run.run_id,
            execution_unit_id=active.unit.execution_unit_id,
            source_attempt_id=active.attempt.attempt_id,
            artifact_id=artifact_id,
            logical_name="synthetic-analysis-report.json",
            classification="INTERNAL",
            content=analysis.report_bytes,
            expected_generation=active.attempt.generation,
        )
        if artifact.state != "READY":
            raise PlatformError("ARTIFACT_NOT_READY", "reference report was not ready")
        await self._persist_artifact_fact(active, artifact)

        evidence_surface = await self.surfaces.commit_revision(
            SurfaceCommitRequest(
                tenant_id=self.tenant_id,
                run_id=active.run.run_id,
                surface_id=f"evidence-{active.run.run_id}",
                source_attempt_id=active.attempt.attempt_id,
                source_generation=active.attempt.generation,
                catalog_id=PUBLIC_CATALOG_ID,
                protocol_version=A2UI_PROTOCOL_VERSION,
                document={
                    "component": "EvidenceSummary",
                    "props": {
                        "title": "Synthetic failure evidence",
                        "data_ref": f"artifact:{artifact_id}:1",
                        "items": [
                            f"{item.source_ref}:{item.signal_code}:{item.source_checksum}"
                            for item in analysis.evidence
                        ],
                    },
                },
                trace_id=active.context.trace_id,
            )
        )
        artifact_surface = await self.surfaces.commit_revision(
            SurfaceCommitRequest(
                tenant_id=self.tenant_id,
                run_id=active.run.run_id,
                surface_id=f"artifact-{active.run.run_id}",
                source_attempt_id=active.attempt.attempt_id,
                source_generation=active.attempt.generation,
                catalog_id=PUBLIC_CATALOG_ID,
                protocol_version=A2UI_PROTOCOL_VERSION,
                document={
                    "component": "ArtifactCard",
                    "props": {
                        "title": "Synthetic analysis report",
                        "artifact_id": artifact_id,
                        "version": artifact.version,
                        "download_action_ref": f"artifact:{artifact_id}:download",
                    },
                },
                trace_id=active.context.trace_id,
            )
        )
        step_id = f"step-{active.run.run_id}"
        action_proposal = await self._prepare_approval_facts(
            active,
            analysis=analysis,
            step_id=step_id,
        )
        before_pause = await self.store.get_run(self.tenant_id, active.run.run_id)
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
                    "node": "await-defect-approval",
                    "report_artifact_id": artifact_id,
                },
                completed_step_ids=("read-synthetic-results", "analyze-failures"),
                active_step_context={"step_id": step_id, "phase": "approval"},
                output_artifact_versions=(
                    ArtifactVersionRef(artifact_id=artifact_id, version=artifact.version),
                ),
                resolved_tool_call_ids=(read.invocation.result_ref,),
                checksum=_digest(
                    {
                        "report_checksum": analysis.report_checksum,
                        "proposal_digest": action_proposal.request_digest,
                    }
                ),
            ),
            approval=ApprovalPause(
                step_id=step_id,
                action_ref=action_proposal.action_ref,
                approval_type="EXTERNAL_WRITE",
                request_digest=action_proposal.request_digest,
                canonical_request_ref=action_proposal.payload_ref,
                expires_at=self.clock() + timedelta(hours=1),
            ),
        )
        approval_surface = await self.surfaces.commit_approval_surface(
            ApprovalSurfaceRequest(
                tenant_id=self.tenant_id,
                run_id=active.run.run_id,
                surface_id=f"approval-{active.run.run_id}",
                approval_id=result.approval.approval_id,
                title="Create a synthetic reference defect?",
                trace_id=active.context.trace_id,
            )
        )
        return PausedReferenceRun(
            context=active.context,
            run=await self.store.get_run(self.tenant_id, active.run.run_id),
            unit=await self.store.get_execution_unit(self.tenant_id, active.unit.execution_unit_id),
            checkpoint=result.checkpoint,
            attempt=await self.store.get_attempt(self.tenant_id, active.attempt.attempt_id),
            lease=await self.store.get_lease_for_attempt(self.tenant_id, active.attempt.attempt_id),
            approval=result.approval,
            action_proposal=action_proposal,
            analysis=analysis,
            artifact=artifact,
            evidence_surface=evidence_surface,
            artifact_surface=artifact_surface,
            approval_surface=approval_surface,
            disconnect_after_event_seq=before_pause.last_event_seq,
        )

    async def _persist_artifact_fact(
        self,
        active: ActiveReferenceRuntime,
        artifact: PublishedArtifactVersion,
    ) -> None:
        async with self.store.transaction() as tx:
            now = await tx.db_now()
            await tx.insert_artifact(
                ArtifactRecord(
                    tenant_id=self.tenant_id,
                    artifact_id=artifact.artifact_id,
                    run_id=active.run.run_id,
                    logical_name=artifact.logical_name,
                    artifact_type="analysis-report",
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
                    lineage={"source": REFERENCE_RESOURCE_REF},
                    created_at=now,
                    ready_at=now,
                )
            )

    async def _prepare_approval_facts(
        self,
        active: ActiveReferenceRuntime,
        *,
        analysis: SyntheticAnalysis,
        step_id: str,
    ) -> ActionProposalRecord:
        if (
            analysis.defect.tool_name != REFERENCE_DEFECT_TOOL_NAME
            or analysis.defect.tool_version != REFERENCE_DEFECT_TOOL_VERSION
            or analysis.defect.connector_name != REFERENCE_DEFECT_CONNECTOR_NAME
            or analysis.defect.canonical_target != REFERENCE_DEFECT_TARGET
        ):
            raise PlatformError(
                "TOOL_SPEC_MISMATCH",
                "reference proposal does not match the trusted defect ToolSpec",
            )
        action_ref = f"action:{active.run.run_id}:defect-create"
        payload_ref = f"restricted:reference-proposal:{active.run.run_id}"
        payload = dict(analysis.defect.canonical_payload)
        payload_digest = _digest(payload)
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
            payload=ResolvedEffectPayload(arguments=payload),
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
                    ordinal=1,
                    name="Propose synthetic defect",
                    step_type="analysis.write-proposal",
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

    async def approve_and_complete(
        self,
        paused: PausedReferenceRun,
        *,
        actor_id: str,
        client_action_id: str,
    ) -> CompletedReferenceRun:
        effect = await self.approve_effect(
            paused,
            actor_id=actor_id,
            client_action_id=client_action_id,
        )
        if effect.state is not EffectState.SUCCEEDED:
            raise PlatformError("EFFECT_NOT_SUCCEEDED", "reference Effect did not succeed")
        successor = await self._claim_successor(paused, effect)
        successor_attempt = await self.terminal.finish_success(
            paused=paused,
            successor=successor,
            effect=effect,
        )
        return CompletedReferenceRun(
            run=await self.store.get_run(self.tenant_id, paused.run.run_id),
            successor_attempt=successor_attempt,
            effect=await self.store.get_effect(self.tenant_id, effect.effect_id),
            approval=await self.store.get_approval_request(
                self.tenant_id, paused.approval.approval_id
            ),
        )

    async def approve_effect(
        self,
        paused: PausedReferenceRun,
        *,
        actor_id: str,
        client_action_id: str,
    ) -> EffectLedgerRecord:
        command = self._approval_command(
            paused,
            decision="APPROVE",
            client_action_id=client_action_id,
        )
        context = self._decision_context(paused, actor_id)
        await self.ui_actions.handle(context, command, idempotency_key=client_action_id)
        effects = await self.store.list_effects(self.tenant_id, paused.run.run_id)
        if len(effects) != 1:
            raise PlatformError(
                "INTEGRITY_VIOLATION", "approval did not prepare exactly one Effect"
            )
        prepared = effects[0]
        token = f"reference-effect:{self.tenant_id}:{prepared.effect_id}"
        self._effect_tokens[prepared.effect_id] = token
        effect = await self.effect_executor.execute(
            self.tenant_id,
            prepared.effect_id,
            token,
            executor_id="reference-effect-worker",
        )
        return effect

    async def reconcile_external_success(
        self,
        effect_id: str,
        *,
        reconciliation_token: str | None = None,
    ) -> EffectLedgerRecord:
        effect = await self.store.get_effect(self.tenant_id, effect_id)
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
                "reference connector has no record for the Effect key",
            )
        result = {
            "remote_id": external_record.defect_id,
            "status": "created",
        }
        evidence_ref = f"reference-defect:{external_record.defect_id}"
        evidence_digest = compute_reconciliation_evidence_digest(
            effect_id=effect.effect_id,
            effect_key=effect.effect_key,
            succeeded=True,
            remote_operation_id=external_record.defect_id,
            result=result,
            evidence_ref=evidence_ref,
        )
        token = reconciliation_token or (
            "reference-reconciliation:"
            f"{self.tenant_id}:{effect.effect_id}:{effect.effect_key}:{evidence_digest}"
        )
        return await self.effect_executor.reconcile(
            self.tenant_id,
            effect_id,
            ReconciledDurableEffect(
                succeeded=True,
                remote_operation_id=external_record.defect_id,
                result=result,
                evidence_ref=evidence_ref,
                evidence_digest=evidence_digest,
            ),
            token,
        )

    async def _claim_successor(
        self,
        paused: PausedReferenceRun,
        effect: EffectLedgerRecord,
    ) -> ActiveReferenceRuntime:
        unit = await self.store.get_execution_unit(self.tenant_id, paused.unit.execution_unit_id)
        run = await self.store.get_run(self.tenant_id, paused.run.run_id)
        if (
            run.status is not RunState.RECOVERING
            or unit.status is not ExecutionUnitState.RECOVERING
            or unit.current_checkpoint_id != paused.checkpoint.checkpoint_id
        ):
            raise PlatformError("INVALID_STATE", "succeeded Effect did not resume checkpoint work")
        reservation = await self.control.reserve_attempt(
            paused.context,
            unit.execution_unit_id,
            paused.checkpoint.checkpoint_id,
            unit.version,
            transition_key=f"effect-resume:{effect.effect_id}",
        )
        lease = await self.control.activate_lease(
            paused.context,
            reservation.attempt.attempt_id,
            reservation.attempt.generation,
            f"reference-runtime:{reservation.attempt.generation}",
            reservation.lease.version,
        )
        return ActiveReferenceRuntime(
            context=paused.context,
            run=await self.store.get_run(self.tenant_id, paused.run.run_id),
            unit=await self.store.get_execution_unit(self.tenant_id, unit.execution_unit_id),
            checkpoint=paused.checkpoint,
            attempt=await self.store.get_attempt(self.tenant_id, reservation.attempt.attempt_id),
            lease=lease,
        )

    async def reject(
        self,
        paused: PausedReferenceRun,
        actor_id: str,
        client_action_id: str,
        reason: str,
    ) -> ApprovalRequestRecord:
        del reason  # shared V1 stores the canonical USER_REJECTED reason code.
        command = self._approval_command(
            paused,
            decision="REJECT",
            client_action_id=client_action_id,
        )
        context = self._decision_context(paused, actor_id)
        await self.ui_actions.handle(context, command, idempotency_key=client_action_id)
        return await self.store.get_approval_request(self.tenant_id, paused.approval.approval_id)

    @staticmethod
    def _approval_command(
        paused: PausedReferenceRun,
        *,
        decision: str,
        client_action_id: str,
    ) -> UiActionCommand:
        props = paused.approval_surface.document.get("props")
        if not isinstance(props, dict):
            raise PlatformError("INTEGRITY_VIOLATION", "approval surface props are invalid")
        action_ref = props.get("approve_key" if decision == "APPROVE" else "reject_key")
        if not isinstance(action_ref, str):
            raise PlatformError("INTEGRITY_VIOLATION", "approval action ref is invalid")
        return UiActionCommand(
            run_id=paused.run.run_id,
            surface_id=paused.approval_surface.surface_id,
            surface_revision=paused.approval_surface.revision.revision,
            action_ref=action_ref,
            client_action_id=client_action_id,
            displayed_digest=paused.approval.request_digest,
        )

    async def redeliver_effect(self, effect_id: str) -> EffectLedgerRecord:
        return await self.effect_executor.execute(
            self.tenant_id,
            effect_id,
            self._effect_tokens[effect_id],
            executor_id="reference-effect-worker",
        )

    async def execute_effect_with_token(self, effect_id: str, token: str) -> EffectLedgerRecord:
        return await self.effect_executor.execute(
            self.tenant_id,
            effect_id,
            token,
            executor_id="reference-effect-worker",
        )

    async def read_report_bytes(self, paused: PausedReferenceRun) -> bytes:
        return await self.object_store.get(paused.artifact.object_key)

    async def replay_events(self, *, run_id: str, after_event_seq: int) -> RunEventPage:
        return await self.queries.get_events(
            self.tenant_id,
            run_id,
            after_event_seq=after_event_seq,
            limit=500,
        )

    async def publish_stale_surface(
        self,
        runtime: ActiveReferenceRuntime | PausedReferenceRun,
    ) -> PublishedSurface:
        return await self.surfaces.commit_revision(
            SurfaceCommitRequest(
                tenant_id=self.tenant_id,
                run_id=runtime.run.run_id,
                surface_id=f"stale-{runtime.run.run_id}",
                source_attempt_id=runtime.attempt.attempt_id,
                source_generation=runtime.attempt.generation,
                catalog_id=PUBLIC_CATALOG_ID,
                protocol_version=A2UI_PROTOCOL_VERSION,
                document={"props": {"status": "stale-runtime-must-not-publish"}},
                trace_id=runtime.context.trace_id,
            )
        )

    async def expire_and_recover(
        self,
        runtime: ActiveReferenceRuntime,
        *,
        message_id: str,
        advance_clock: bool = True,
    ) -> RecoveryResult | None:
        if advance_clock:
            if runtime.lease.expires_at is None:
                raise PlatformError("INTEGRITY_VIOLATION", "active Lease has no expiry")
            self.clock.now = runtime.lease.expires_at + timedelta(seconds=1)
        return await recover_expired_lease(
            self.store,
            runtime.context,
            message_id=message_id,
            handler_version="reference-lease-reconciler/v1",
            attempt_id=runtime.attempt.attempt_id,
            generation=runtime.attempt.generation,
        )

    async def list_effects(self, run_id: str) -> tuple[EffectLedgerRecord, ...]:
        return await self.store.list_effects(self.tenant_id, run_id)

    async def get_effect(self, effect_id: str) -> EffectLedgerRecord:
        return await self.store.get_effect(self.tenant_id, effect_id)

    async def get_run(self, run_id: str) -> RunRecord:
        return await self.store.get_run(self.tenant_id, run_id)

    async def get_unit(self, execution_unit_id: str) -> ExecutionUnitRecord:
        return await self.store.get_execution_unit(self.tenant_id, execution_unit_id)

    async def get_action_proposal(self, action_ref: str) -> ActionProposalRecord:
        return await self.store.get_action_proposal(self.tenant_id, action_ref)

    async def get_attempt(self, attempt_id: str) -> AttemptRecord:
        return await self.store.get_attempt(self.tenant_id, attempt_id)

    async def get_lease(self, attempt_id: str) -> ExecutionLeaseRecord:
        return await self.store.get_lease_for_attempt(self.tenant_id, attempt_id)


__all__ = [
    "ActiveReferenceRuntime",
    "CompletedReferenceRun",
    "PausedReferenceRun",
    "ReadToolExecution",
    "ReferenceEffectPayloadResolver",
    "ReferenceTerminalAdapter",
    "ReferenceWorkflowHarness",
    "SyntheticReadProvider",
]
