"""Durable event wire envelope."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .enums import (
    ApprovalState,
    AttemptState,
    EffectState,
    EventType,
    RunState,
    ToolInvocationState,
)
from .models import JsonValue, RunEventPage, StrictModel


class RunCreatedPayload(StrictModel):
    kind: Literal["run.created"]
    workflow_type: str


class RunStatusChangedPayload(StrictModel):
    kind: Literal["run.status.changed"]
    previous: RunState
    current: RunState


class AttemptLifecyclePayload(StrictModel):
    kind: Literal["attempt.lifecycle"]
    attempt_id: str
    status: AttemptState


class ToolInvocationRecordedPayload(StrictModel):
    kind: Literal["tool.invocation.recorded"]
    call_id: str
    status: ToolInvocationState


class ApprovalDecidedPayload(StrictModel):
    kind: Literal["approval.decided"]
    approval_id: str
    status: ApprovalState


class EffectStatusChangedPayload(StrictModel):
    kind: Literal["effect.status.changed"]
    effect_id: str
    status: EffectState


class UiSurfaceCommittedPayload(StrictModel):
    kind: Literal["ui.surface.committed"]
    surface_id: str
    revision: Annotated[int, Field(ge=1)]


class ArtifactVersionPayload(StrictModel):
    kind: Literal["artifact.version"]
    artifact_id: str
    run_id: str
    logical_name: str
    classification: str
    version: Annotated[int, Field(ge=1)]
    state: str


class ActionProposalPayload(StrictModel):
    kind: Literal["action.proposal"]
    action_ref: str
    run_id: str
    attempt_id: str
    proposal_state: str
    risk_class: str


class ToolCallSummary(StrictModel):
    """One aggregated tool call inside an ``agent.turn.completed`` payload."""

    call_id: str
    tool_name: str
    status: Literal["succeeded", "failed"]
    is_error: bool = False


class ToolExecutionStartedPayload(StrictModel):
    """Persistent bridge event: pi-agent-core ToolExecutionStartEvent.

    Durable (append_event -> PG -> Outbox -> SSE) so a reconnecting frontend
    can render the tool activity from the event log alone. ``args`` is the
    bounded, redacted-at-source argument snapshot; never raw credentials.
    """

    kind: Literal["tool.execution.started"]
    call_id: str
    tool_name: str
    args: dict[str, JsonValue] | None = None


class ToolExecutionEndedPayload(StrictModel):
    """Persistent bridge event: pi-agent-core ToolExecutionEndEvent."""

    kind: Literal["tool.execution.ended"]
    call_id: str
    tool_name: str
    status: Literal["succeeded", "failed"]
    is_error: bool = False
    result: dict[str, JsonValue] | None = None


class AgentTurnCompletedPayload(StrictModel):
    """Persistent bridge event: full-turn aggregation (SDD §11.4).

    Carries the complete aggregated thinking, the complete assistant message
    text and the tool-call summary for one turn. Replay-safe: a freshly
    opened or reconnected frontend renders the turn from this single event
    instead of thousands of ephemeral stream-chunks.
    """

    kind: Literal["agent.turn.completed"]
    turn_seq: Annotated[int, Field(ge=1)]
    thinking: str = ""
    message_text: str = ""
    tool_calls: tuple[ToolCallSummary, ...] = ()


EventPayload = Annotated[
    RunCreatedPayload
    | RunStatusChangedPayload
    | AttemptLifecyclePayload
    | ToolInvocationRecordedPayload
    | ApprovalDecidedPayload
    | EffectStatusChangedPayload
    | UiSurfaceCommittedPayload
    | ArtifactVersionPayload
    | ActionProposalPayload
    | ToolExecutionStartedPayload
    | ToolExecutionEndedPayload
    | AgentTurnCompletedPayload,
    Field(discriminator="kind"),
]

EVENT_PAYLOAD_CONTRACTS: dict[EventType, tuple[type[StrictModel], str]] = {
    EventType.RUN_CREATED: (RunCreatedPayload, "run-created/v1"),
    EventType.RUN_STATUS_CHANGED: (RunStatusChangedPayload, "run-status/v1"),
    EventType.ATTEMPT_LIFECYCLE: (AttemptLifecyclePayload, "attempt-lifecycle/v1"),
    EventType.TOOL_INVOCATION_RECORDED: (
        ToolInvocationRecordedPayload,
        "tool-invocation/v1",
    ),
    EventType.APPROVAL_DECIDED: (ApprovalDecidedPayload, "approval/v1"),
    EventType.EFFECT_STATUS_CHANGED: (EffectStatusChangedPayload, "effect/v1"),
    EventType.UI_SURFACE_COMMITTED: (UiSurfaceCommittedPayload, "a2ui-surface/v0.9.1"),
    EventType.ARTIFACT_VERSION: (ArtifactVersionPayload, "artifact-version/v1"),
    EventType.ACTION_PROPOSAL: (ActionProposalPayload, "action-proposal/v1"),
    EventType.TOOL_EXECUTION_STARTED: (
        ToolExecutionStartedPayload,
        "tool-execution/v1",
    ),
    EventType.TOOL_EXECUTION_ENDED: (
        ToolExecutionEndedPayload,
        "tool-execution/v1",
    ),
    EventType.AGENT_TURN_COMPLETED: (
        AgentTurnCompletedPayload,
        "agent-turn/v1",
    ),
}


class EnterpriseEventEnvelope(StrictModel):
    schema_version: Literal["enterprise-event/v1"]
    event_id: str
    tenant_id: str
    run_id: str
    event_seq: Annotated[int, Field(ge=1)]
    event_type: EventType
    occurred_at: datetime
    producer_service: str
    payload_schema: str
    payload: EventPayload
    attempt_id: str | None = None
    causation_event_id: str | None = None
    trace_id: str | None = None

    @model_validator(mode="after")
    def validate_payload_contract(self) -> "EnterpriseEventEnvelope":
        payload_type, payload_schema = EVENT_PAYLOAD_CONTRACTS[self.event_type]
        if type(self.payload) is not payload_type or self.payload_schema != payload_schema:
            raise ValueError(
                f"{self.event_type.value} requires {payload_type.__name__} and {payload_schema}"
            )
        return self


RunEventPage.model_rebuild(_types_namespace={"EnterpriseEventEnvelope": EnterpriseEventEnvelope})
