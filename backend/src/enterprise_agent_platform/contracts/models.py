"""Strict serializable objects shared across platform boundaries."""
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .enums import (
    ApprovalState,
    AttemptState,
    EffectCapabilityAudience,
    ExecutionUnitState,
    RunState,
    RuntimeCapabilityAudience,
    StepState,
    ToolInvocationState,
    ToolRiskClass,
)

if TYPE_CHECKING:
    from .events import EnterpriseEventEnvelope


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeCapabilityClaims(StrictModel):
    schema_version: Literal["runtime-capability/v1"]
    token_id: str
    tenant_id: str
    run_id: str
    execution_unit_id: str
    attempt_id: str
    generation: Annotated[int, Field(ge=1)]
    audience: RuntimeCapabilityAudience
    scopes: tuple[str, ...]
    expires_at: datetime


class EffectCapabilityClaims(StrictModel):
    schema_version: Literal["effect-capability/v1"]
    token_id: str
    tenant_id: str
    effect_id: str
    approval_id: str
    request_digest: str
    tool_name: str
    tool_version: str
    tool_spec_digest: str
    connector_name: str
    canonical_target: str
    scopes: tuple[str, ...]
    audience: EffectCapabilityAudience
    expires_at: datetime


class ToolInvocation(StrictModel):
    schema_version: Literal["tool-invocation/v1"]
    call_id: str
    attempt_id: str
    generation: Annotated[int, Field(ge=1)]
    tool_name: str
    tool_spec_version: str
    grant_id: str
    input_digest: str
    status: ToolInvocationState
    risk_class: ToolRiskClass
    resource_ref: str
    result_ref: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class Approval(StrictModel):
    schema_version: Literal["approval/v1"]
    approval_id: str
    run_id: str
    request_digest: str
    status: ApprovalState
    version: Annotated[int, Field(ge=1)]
    canonical_request_ref: str
    expires_at: datetime
    decided_by: str | None = None


class UiSurface(StrictModel):
    schema_version: Literal["a2ui-surface/v0.9.1"]
    surface_id: str
    run_id: str
    catalog_id: str
    revision: Annotated[int, Field(ge=1)]
    source_event_seq: Annotated[int, Field(ge=1)]
    document: dict[str, JsonValue]


class SurfaceRevision(StrictModel):
    schema_version: Literal["a2ui-surface-revision/v0.9.1"]
    surface_id: str
    run_id: str
    revision: Annotated[int, Field(ge=1)]
    source_attempt_id: str
    source_event_seq: Annotated[int, Field(ge=1)]
    document: dict[str, JsonValue]
    checksum: str


class ExecutionUnitSummary(StrictModel):
    execution_unit_id: str
    role: str
    status: ExecutionUnitState
    version: Annotated[int, Field(ge=1)]


class AttemptSummary(StrictModel):
    attempt_id: str
    execution_unit_id: str
    step_id: str | None
    status: AttemptState
    version: Annotated[int, Field(ge=1)]
    started_at: datetime | None
    ended_at: datetime | None


class StepSummary(StrictModel):
    step_id: str
    name: str
    status: StepState
    version: Annotated[int, Field(ge=1)]


class ApprovalSummary(StrictModel):
    approval_id: str
    status: ApprovalState
    version: Annotated[int, Field(ge=1)]


class ArtifactSummary(StrictModel):
    artifact_id: str
    name: str
    media_type: str
    version: Annotated[int, Field(ge=1)]


class SurfaceSummary(StrictModel):
    surface_id: str
    catalog_id: str
    revision: Annotated[int, Field(ge=1)]


class RunView(StrictModel):
    run_id: str
    parent_run_id: str | None
    workflow_type: str
    intent: str
    status: RunState
    status_reason: str | None
    version: Annotated[int, Field(ge=1)]
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    execution_units: tuple[ExecutionUnitSummary, ...]
    attempts: tuple[AttemptSummary, ...]
    current_step: StepSummary | None = None
    approvals: tuple[ApprovalSummary, ...] = ()
    artifacts: tuple[ArtifactSummary, ...] = ()
    surfaces: tuple[SurfaceSummary, ...] = ()
    watermark: Annotated[int, Field(ge=0)]


class RunViewSnapshot(StrictModel):
    schema_version: Literal["run-view-snapshot/v1"]
    run_id: str
    status: RunState
    watermark: Annotated[int, Field(ge=0)]
    view: RunView


class RunEventPage(StrictModel):
    schema_version: Literal["run-event-page/v1"]
    run_id: str
    after_event_seq: Annotated[int, Field(ge=0)]
    watermark: Annotated[int, Field(ge=0)]
    retention_floor: Annotated[int, Field(ge=0)]
    resync_required: Literal[False]
    events: tuple["EnterpriseEventEnvelope", ...]

    @model_validator(mode="after")
    def validate_replay_window(self) -> "RunEventPage":
        if self.retention_floor > self.watermark:
            raise ValueError("retention floor cannot exceed the page watermark")
        if self.after_event_seq > self.watermark:
            raise ValueError("replay cursor cannot exceed the page watermark")
        cursor_precedes_retention = self.after_event_seq < self.retention_floor
        if cursor_precedes_retention:
            raise ValueError("cursor precedes retention floor; resync is required")
        previous_seq = self.after_event_seq
        for event in self.events:
            if event.run_id != self.run_id:
                raise ValueError("replay page cannot contain events from another run")
            if event.event_seq <= previous_seq:
                raise ValueError("replay event_seq must be strictly increasing after the cursor")
            if event.event_seq > self.watermark:
                raise ValueError("replay event_seq cannot exceed the page watermark")
            previous_seq = event.event_seq
        return self


class ArtifactDownloadAuthorization(StrictModel):
    schema_version: Literal["artifact-download-authorization/v1"]
    authorization_id: str
    artifact_id: str
    version: Annotated[int, Field(ge=1)]
    download_url: str
    expires_at: datetime
