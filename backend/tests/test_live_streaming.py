"""Live-streaming bridge tests (SDD §11.4 / §11.5).

Covers the three layers of the dual-link design:

1. Durable bridge events — new EnterpriseEventEnvelope payload contracts
   (tool.execution.started / tool.execution.ended / agent.turn.completed) and
   the strict payload_schema guard.
2. Runtime bridge — AgentRuntime._on_agent_event maps pi-agent-core
   ToolExecutionStart/Update/End + StreamThinking/TextDelta + TurnEnd onto
   the durable emit / ephemeral stream_chunk sink.
3. Parent side — SubprocessOrchestrator OP_EMIT_EVENT appends a durable
   envelope (allowlist + contract validation), OP_STREAM_CHUNK pushes only to
   the in-memory relay; the relay is bounded; SSE chunk frames are framed
   without event_seq and never enter the replay page.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from pi_agent_core.types import (
    AssistantMessage,
    StreamTextDeltaEvent,
    StreamThinkingDeltaEvent,
    TextContent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
)

from enterprise_agent_platform.contracts.enums import EventType
from enterprise_agent_platform.contracts.events import (
    AgentTurnCompletedPayload,
    EVENT_PAYLOAD_CONTRACTS,
    EnterpriseEventEnvelope,
    ToolExecutionEndedPayload,
    ToolExecutionStartedPayload,
)
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.domain.records import DispatchTicket
from enterprise_agent_platform.execution.runtime import (
    _capsize_args,
    _clip_text,
    AgentRuntime,
)
from enterprise_agent_platform.execution.subprocess_orchestrator import (
    SubprocessOrchestrator,
)
from enterprise_agent_platform.fastapi.sse import chunk_frame
from enterprise_agent_platform.persistence.protocol import PlatformError
from enterprise_agent_platform.platform.run_chunks import InMemoryRunChunkRelay
from enterprise_agent_platform.reference.provider import ReferenceWorkflowHarness


def _now() -> datetime:
    return datetime.now(UTC)


def _ticket(active) -> DispatchTicket:
    return DispatchTicket(
        worker_id="test-worker",
        tenant_id=active.run.tenant_id,
        run_id=active.run.run_id,
        execution_unit_id=active.unit.execution_unit_id,
        attempt_id=active.attempt.attempt_id,
        lease_id=active.lease.lease_id,
        generation=active.attempt.generation,
        source_checkpoint_id=active.checkpoint.checkpoint_id,
    )


def _ctx(tenant_id: str) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        actor_id="test-runtime",
        scopes=("runs:execute",),
        request_id="test-request",
        trace_id="test-trace",
    )


def _orchestrator(harness: ReferenceWorkflowHarness, relay) -> SubprocessOrchestrator:
    return SubprocessOrchestrator(
        store=harness.store,
        control=harness.control,
        chunk_relay=relay,
        python="python",  # never spawned in these tests
    )


# ---------------------------------------------------------------------------
# 1. Durable event contracts
# ---------------------------------------------------------------------------


def test_bridge_event_contracts_are_registered() -> None:
    assert EventType.TOOL_EXECUTION_STARTED in EVENT_PAYLOAD_CONTRACTS
    assert EventType.TOOL_EXECUTION_ENDED in EVENT_PAYLOAD_CONTRACTS
    assert EventType.AGENT_TURN_COMPLETED in EVENT_PAYLOAD_CONTRACTS
    assert EVENT_PAYLOAD_CONTRACTS[EventType.TOOL_EXECUTION_STARTED][1] == "tool-execution/v1"
    assert EVENT_PAYLOAD_CONTRACTS[EventType.AGENT_TURN_COMPLETED][1] == "agent-turn/v1"


def test_bridge_envelopes_pass_strict_contract_guard() -> None:
    now = _now()
    started = EnterpriseEventEnvelope(
        schema_version="enterprise-event/v1",
        event_id="evt_started",
        tenant_id="t",
        run_id="r",
        event_seq=1,
        event_type=EventType.TOOL_EXECUTION_STARTED,
        occurred_at=now,
        producer_service="runtime-child",
        payload_schema="tool-execution/v1",
        payload=ToolExecutionStartedPayload(
            kind="tool.execution.started",
            call_id="c1",
            tool_name="synthetic.results.read",
            args={"max_items": 100},
        ),
        attempt_id="a1",
    )
    ended = EnterpriseEventEnvelope(
        schema_version="enterprise-event/v1",
        event_id="evt_ended",
        tenant_id="t",
        run_id="r",
        event_seq=2,
        event_type=EventType.TOOL_EXECUTION_ENDED,
        occurred_at=now,
        producer_service="runtime-child",
        payload_schema="tool-execution/v1",
        payload=ToolExecutionEndedPayload(
            kind="tool.execution.ended",
            call_id="c1",
            tool_name="synthetic.results.read",
            status="succeeded",
            is_error=False,
            result={"case_count": 100},
        ),
        attempt_id="a1",
    )
    turn = EnterpriseEventEnvelope(
        schema_version="enterprise-event/v1",
        event_id="evt_turn",
        tenant_id="t",
        run_id="r",
        event_seq=3,
        event_type=EventType.AGENT_TURN_COMPLETED,
        occurred_at=now,
        producer_service="runtime-child",
        payload_schema="agent-turn/v1",
        payload=AgentTurnCompletedPayload(
            kind="agent.turn.completed",
            turn_seq=1,
            thinking="先读取数据集…",
            message_text="分析完成",
            tool_calls=(),
        ),
        attempt_id="a1",
    )
    # All three envelopes validate (schema+payload-type guard is satisfied).
    for envelope in (started, ended, turn):
        assert envelope.validate_payload_contract() is not None


def test_bridge_envelope_rejects_mismatched_payload_schema() -> None:
    with pytest.raises(ValueError):
        EnterpriseEventEnvelope(
            schema_version="enterprise-event/v1",
            event_id="evt_bad",
            tenant_id="t",
            run_id="r",
            event_seq=1,
            event_type=EventType.TOOL_EXECUTION_STARTED,
            occurred_at=_now(),
            producer_service="runtime-child",
            payload_schema="agent-turn/v1",  # wrong schema for this event type
            payload=AgentTurnCompletedPayload(
                kind="agent.turn.completed",
                turn_seq=1,
                thinking="",
                message_text="",
                tool_calls=(),
            ),
            attempt_id="a1",
        )


# ---------------------------------------------------------------------------
# 2. Runtime bridge (_on_agent_event -> sink)
# ---------------------------------------------------------------------------


class _FakeBootstrap:
    async def claim(self, **kwargs):
        del kwargs
        raise AssertionError("claim must not be called in bridge tests")


class _FakeControl:
    async def restore(self, context):
        del context
        raise AssertionError("restore must not be called in bridge tests")

    async def heartbeat(self, context):
        return context

    async def commit_checkpoint(self, context, *, agent_state, agent_state_schema_version):
        del agent_state, agent_state_schema_version
        return None

    async def commit_final_checkpoint(
        self, context, *, summary, agent_state=None, agent_state_schema_version=None
    ):
        del summary, agent_state, agent_state_schema_version
        return None

    async def record_failure(self, context, *, reason_code, retryable):
        del reason_code, retryable
        return None


class _RecordingSink:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []
        self.chunks: list[dict] = []

    async def emit_event(self, *, event_type: str, payload: dict) -> None:
        self.emitted.append((event_type, payload))

    async def stream_chunk(self, *, chunk: dict) -> None:
        self.chunks.append(chunk)


class _DummyAgent:
    class _State:
        def model_dump(self) -> dict:
            return {"messages": [], "system_prompt": "", "model": {}, "thinking_level": "off"}

    state = _State()


@pytest.mark.asyncio
async def test_on_agent_event_tool_execution_bridge() -> None:
    runtime = AgentRuntime(_FakeBootstrap(), _FakeControl())
    sink = _RecordingSink()
    runtime.set_event_sink(sink)
    context = runtime._context = None  # only used by TurnEnd heartbeat; not here

    runtime._on_agent_event(
        ToolExecutionStartEvent(
            tool_call_id="c1",
            tool_name="synthetic.results.read",
            args={"api_key": "sk-secret", "max_items": 100},
        ),
        context,  # type: ignore[arg-type]
        _DummyAgent(),
    )
    runtime._on_agent_event(
        ToolExecutionUpdateEvent(
            tool_call_id="c1",
            tool_name="synthetic.results.read",
            partial_result={"progress": "50%"},
        ),
        context,  # type: ignore[arg-type]
        _DummyAgent(),
    )
    runtime._on_agent_event(
        ToolExecutionEndEvent(
            tool_call_id="c1",
            tool_name="synthetic.results.read",
            result={"case_count": 100},
            is_error=False,
        ),
        context,  # type: ignore[arg-type]
        _DummyAgent(),
    )
    await asyncio.sleep(0)  # let spawned emit/chunk tasks run

    event_types = [event_type for event_type, _payload in sink.emitted]
    assert event_types == ["tool.execution.started", "tool.execution.ended"]
    started_payload = sink.emitted[0][1]
    # Credential-like key is redacted at the source.
    assert started_payload["args"] == {"api_key": "[REDACTED]", "max_items": 100}
    ended_payload = sink.emitted[1][1]
    assert ended_payload["status"] == "succeeded"

    kinds = [chunk["kind"] for chunk in sink.chunks]
    assert kinds == [
        "tool.execution.started",
        "tool.execution.updated",
        "tool.execution.ended",
    ]
    assert sink.chunks[1]["partial"] is not None


@pytest.mark.asyncio
async def test_on_agent_event_turn_aggregation() -> None:
    runtime = AgentRuntime(_FakeBootstrap(), _FakeControl())
    sink = _RecordingSink()
    runtime.set_event_sink(sink)
    context = None

    runtime._on_agent_event(
        StreamThinkingDeltaEvent(content_index=0, delta="首先", partial=AssistantMessage(api="a", provider="p", model="m")),
        context,  # type: ignore[arg-type]
        _DummyAgent(),
    )
    runtime._on_agent_event(
        StreamThinkingDeltaEvent(content_index=0, delta=" 分析", partial=AssistantMessage(api="a", provider="p", model="m")),
        context,  # type: ignore[arg-type]
        _DummyAgent(),
    )
    runtime._on_agent_event(
        StreamTextDeltaEvent(content_index=1, delta="结论是", partial=AssistantMessage(api="a", provider="p", model="m")),
        context,  # type: ignore[arg-type]
        _DummyAgent(),
    )
    runtime._on_agent_event(
        TurnEndEvent(message=AssistantMessage(content=[TextContent(text="结论是")], api="a", provider="p", model="m")),
        context,  # type: ignore[arg-type]
        _DummyAgent(),
    )
    await asyncio.sleep(0)

    kinds = [chunk["kind"] for chunk in sink.chunks]
    assert kinds == ["thinking.delta", "thinking.delta", "text.delta"]
    assert sink.emitted[-1][0] == "agent.turn.completed"
    turn_payload = sink.emitted[-1][1]
    assert turn_payload["turn_seq"] == 1
    assert turn_payload["thinking"] == "首先 分析"
    assert turn_payload["message_text"] == "结论是"


def test_capsize_and_redact_helpers() -> None:
    assert _clip_text("x" * 20_000).endswith("[truncated]")
    assert _capsize_args({"token": "abc", "ok": 1}) == {"token": "[REDACTED]", "ok": 1}
    assert _capsize_args("not-a-dict") is None
    assert _capsize_args(None) is None


# ---------------------------------------------------------------------------
# 3. Parent side: orchestrator ops + relay + SSE chunk frames
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_emit_event_appends_durable_envelope() -> None:
    harness = ReferenceWorkflowHarness()
    active = await harness.start_active_runtime()
    relay = InMemoryRunChunkRelay()
    orchestrator = _orchestrator(harness, relay)
    ticket = _ticket(active)
    ctx = _ctx(active.run.tenant_id)

    before = await harness.get_run(active.run.run_id)
    result = await orchestrator._op_emit_event(
        ticket,
        ctx,
        {
            "event_type": "tool.execution.started",
            "payload": {
                "kind": "tool.execution.started",
                "call_id": "c1",
                "tool_name": "synthetic.results.read",
                "args": {"max_items": 100},
            },
        },
    )
    assert result["status"] == "accepted"

    after = await harness.get_run(active.run.run_id)
    assert after.last_event_seq == before.last_event_seq + 1
    page = await harness.replay_events(
        run_id=active.run.run_id, after_event_seq=before.last_event_seq
    )
    assert len(page.events) == 1
    event = page.events[0]
    assert event.event_type is EventType.TOOL_EXECUTION_STARTED
    assert event.attempt_id == ticket.attempt_id
    assert event.payload.kind == "tool.execution.started"
    # Relay untouched by the durable link.
    assert relay.pending(active.run.run_id) == 0


@pytest.mark.asyncio
async def test_orchestrator_emit_event_rejects_non_bridge_types() -> None:
    harness = ReferenceWorkflowHarness()
    active = await harness.start_active_runtime()
    orchestrator = _orchestrator(harness, InMemoryRunChunkRelay())
    ticket = _ticket(active)
    ctx = _ctx(active.run.tenant_id)
    with pytest.raises(PlatformError) as excinfo:
        await orchestrator._op_emit_event(
            ticket,
            ctx,
            {
                "event_type": "run.created",  # not in the bridge allowlist
                "payload": {"kind": "run.created", "workflow_type": "x"},
            },
        )
    assert excinfo.value.code == "EVENT_TYPE_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_orchestrator_emit_event_rejects_invalid_payload() -> None:
    harness = ReferenceWorkflowHarness()
    active = await harness.start_active_runtime()
    orchestrator = _orchestrator(harness, InMemoryRunChunkRelay())
    ticket = _ticket(active)
    ctx = _ctx(active.run.tenant_id)
    with pytest.raises(PlatformError) as excinfo:
        await orchestrator._op_emit_event(
            ticket,
            ctx,
            {
                "event_type": "tool.execution.started",
                "payload": {"kind": "tool.execution.ended", "call_id": "c1"},  # wrong kind
            },
        )
    assert excinfo.value.code == "INVALID_EVENT_PAYLOAD"


@pytest.mark.asyncio
async def test_orchestrator_stream_chunk_goes_only_to_relay() -> None:
    harness = ReferenceWorkflowHarness()
    active = await harness.start_active_runtime()
    relay = InMemoryRunChunkRelay()
    orchestrator = _orchestrator(harness, relay)
    ticket = _ticket(active)

    await orchestrator._op_stream_chunk(
        ticket, {"chunk": {"kind": "thinking.delta", "delta": "正在分析…"}}
    )
    drained = relay.drain(active.run.run_id, limit=100)
    assert len(drained) == 1
    assert drained[0]["kind"] == "thinking.delta"
    assert drained[0]["run_id"] == active.run.run_id  # parent stamps authority
    # No durable event appended for an ephemeral chunk.
    page = await harness.replay_events(
        run_id=active.run.run_id, after_event_seq=active.run.last_event_seq
    )
    assert page.events == ()


def test_relay_is_bounded_and_evicts_oldest() -> None:
    relay = InMemoryRunChunkRelay(max_chunks_per_run=3)
    for index in range(5):
        relay.push("r1", {"kind": "text.delta", "delta": str(index)})
    drained = relay.drain("r1", limit=100)
    assert [item["delta"] for item in drained] == ["2", "3", "4"]
    assert relay.pending("r1") == 0


def test_relay_evicts_oldest_run_when_capacity_exhausted() -> None:
    relay = InMemoryRunChunkRelay(max_runs=2)
    relay.push("r1", {"kind": "text.delta", "delta": "1"})
    relay.push("r2", {"kind": "text.delta", "delta": "2"})
    relay.push("r3", {"kind": "text.delta", "delta": "3"})
    assert relay.drain("r1", limit=10) == []
    assert relay.pending("r2") == 1
    assert relay.pending("r3") == 1


def test_sse_chunk_frame_has_no_event_seq() -> None:
    frame = chunk_frame({"run_id": "r1", "kind": "thinking.delta", "delta": "分析"})
    assert frame.startswith("event: stream-chunk\n")
    assert "id:" not in frame.split("\n", 1)[0]
    assert "event_seq" not in frame
    assert "分析" in frame