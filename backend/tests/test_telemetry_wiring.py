"""Phase 4.3 (G5) unit tests: telemetry wiring — trace context, channels, sinks.

Covers:
- Explicit trace ids + parent/child chaining (SDD G.4 主链路)
- Bounded span/metric policy (attribute + label registries)
- histogram / gauge channels and env-gated assembly
- OTel sink trace-id preservation (InMemorySpanExporter)
- Prometheus sink text export (counter / gauge / histogram)
- TelemetrySessionProvider wrapper (model.call RED by model)
- FastAPI RED middleware + /metrics route
- JSON log correlation fields (Loki join key)
"""
from __future__ import annotations

import asyncio
import io
import logging

import pytest
from fastapi.testclient import TestClient

from enterprise_agent_platform import create_app
from enterprise_agent_platform.platform.logging_json import (
    JsonLogFormatter,
    correlation,
)
from enterprise_agent_platform.platform.telemetry import (
    DiagnosticTelemetry,
    InMemoryDiagnosticSink,
    OpenTelemetryDiagnosticSink,
    PrometheusMetricSink,
    TelemetryPolicyError,
)
from enterprise_agent_platform.platform.telemetry_service import (
    TelemetrySessionProvider,
    create_telemetry_from_env,
    default_metric_label_policy,
    http_route_bucket,
    http_status_class,
    maybe_wrap_sessions,
    prometheus_enabled,
)
from enterprise_agent_platform.reference.local_stack import create_container


def _telemetry(sink: InMemoryDiagnosticSink | None = None) -> DiagnosticTelemetry:
    return DiagnosticTelemetry(
        sink=sink or InMemoryDiagnosticSink(),
        metric_labels=default_metric_label_policy(),
    )


# ---------------------------------------------------------------------------
# Trace context
# ---------------------------------------------------------------------------


def test_span_parent_child_share_trace_id() -> None:
    sink = InMemoryDiagnosticSink()
    tele = _telemetry(sink)
    trace = tele.begin_trace()
    with tele.span(
        "run.created",
        attributes={
            "agent.platform.tenant.id": "demo-tenant",
            "agent.platform.workflow.class": "business-analysis",
        },
        trace=trace,
    ) as root, tele.span(
        "attempt.dispatch",
        attributes={
            "agent.platform.run.id": "run_1",
            "agent.platform.attempt.id": "attempt_1",
            "agent.platform.lease.generation": 1,
        },
        trace=trace,
        parent=root,
    ):
        pass
    traces = [e for e in sink.events if e.channel == "trace"]
    assert len(traces) == 2
    by_name = {e.name: e for e in traces}
    root = by_name["run.created"]
    child = by_name["attempt.dispatch"]
    assert root.trace_id == child.trace_id
    assert root.parent_span_id is None
    assert child.parent_span_id == root.span_id
    assert child.span_id != root.span_id


def test_begin_trace_reuses_inbound_trace_id() -> None:
    tele = _telemetry()
    inbound = "a" * 32
    handle = tele.begin_trace(trace_id=inbound)
    assert handle.trace_id == inbound


def test_begin_trace_rejects_malformed_trace_id() -> None:
    tele = _telemetry()
    with pytest.raises(TelemetryPolicyError):
        tele.begin_trace(trace_id="not-a-trace-id")


def test_span_rejects_unbounded_name() -> None:
    tele = _telemetry()
    with pytest.raises(TelemetryPolicyError), tele.span(
        "user.freeform", trace=tele.begin_trace()
    ):
        pass


def test_span_rejects_unregistered_attribute() -> None:
    tele = _telemetry()
    with pytest.raises(TelemetryPolicyError), tele.span(
        "run.created", attributes={"user.private.field": "x"}
    ):
        pass


def test_span_captures_exception_type() -> None:
    sink = InMemoryDiagnosticSink()
    tele = _telemetry(sink)
    with pytest.raises(RuntimeError), tele.span("run.created"):
        raise RuntimeError("boom")
    event = next(e for e in sink.events if e.channel == "trace")
    assert ("exception.type", "RuntimeError") in event.attributes


# ---------------------------------------------------------------------------
# Metric policy + channels
# ---------------------------------------------------------------------------


def test_metric_policy_rejects_unregistered_label() -> None:
    tele = _telemetry()
    with pytest.raises(TelemetryPolicyError):
        tele.record_metric("x_total", 1.0, labels={"user_cardinality": "a"})


def test_metric_policy_rejects_unregistered_value() -> None:
    tele = _telemetry()
    with pytest.raises(TelemetryPolicyError):
        tele.record_metric("agent_platform_run_lifecycle_total", 1.0, labels={"state": "exploded"})


def test_histogram_and_gauge_channels_recorded() -> None:
    sink = InMemoryDiagnosticSink()
    tele = _telemetry(sink)
    assert tele.record_gauge("agent_platform_queue_backlog", 3.0, labels={"operation": "scheduler.claim"})
    assert tele.timing(
        "agent_platform_model_latency_seconds",
        0.5,
        labels={"model_id": "deepseek-chat", "outcome": "ok"},
    )
    channels = {e.channel for e in sink.events}
    assert {"gauge", "histogram"} <= channels


def test_timing_rejects_unbounded_histogram_name() -> None:
    tele = _telemetry()
    with pytest.raises(TelemetryPolicyError):
        tele.timing("user_defined_latency", 1.0, labels={"state": "queued"})


def test_zero_tolerance_signal_validates_signal() -> None:
    tele = _telemetry()
    with pytest.raises(TelemetryPolicyError):
        tele.record_zero_tolerance("not-a-signal")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Env-gated assembly
# ---------------------------------------------------------------------------


def test_create_telemetry_from_env_none_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_PLATFORM_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("AGENT_PLATFORM_PROMETHEUS_ENABLED", raising=False)
    assert create_telemetry_from_env() is None


def test_create_telemetry_from_env_prometheus_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_PLATFORM_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("AGENT_PLATFORM_PROMETHEUS_ENABLED", "1")
    tele = create_telemetry_from_env(service_name="test")
    assert tele is not None
    assert prometheus_enabled(tele)


def test_create_telemetry_from_env_otlp_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PLATFORM_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.delenv("AGENT_PLATFORM_PROMETHEUS_ENABLED", raising=False)
    tele = create_telemetry_from_env(service_name="test")
    assert tele is not None


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


def test_prometheus_sink_exports_counter_gauge_histogram() -> None:
    from prometheus_client import CollectorRegistry, generate_latest

    registry = CollectorRegistry()
    sink = PrometheusMetricSink(registry=registry)
    tele = DiagnosticTelemetry(sink=sink, metric_labels=default_metric_label_policy())
    tele.record_metric("agent_platform_run_lifecycle_total", 1.0, labels={"state": "queued"})
    tele.record_gauge("agent_platform_queue_backlog", 3.0, labels={"operation": "scheduler.claim"})
    tele.timing(
        "agent_platform_http_latency_seconds",
        0.05,
        labels={"http.route": "/v1/runs", "http.status_class": "2xx"},
    )
    text = generate_latest(registry).decode()
    assert "agent_platform_run_lifecycle_total{state=\"queued\"} 1.0" in text
    assert "agent_platform_queue_backlog{operation=\"scheduler.claim\"} 3.0" in text
    assert "agent_platform_http_latency_seconds_bucket" in text


def test_otel_sink_preserves_explicit_trace_ids() -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    resource = Resource.create({"service.name": "test"})
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(resource=resource, metric_readers=(metric_reader,))
    sink = OpenTelemetryDiagnosticSink(
        tracer=tracer_provider.get_tracer("test"),
        meter=meter_provider.get_meter("test"),
    )
    tele = DiagnosticTelemetry(sink=sink, metric_labels=default_metric_label_policy())

    trace = tele.begin_trace()
    with tele.span("run.created", trace=trace) as root, tele.span(
        "model.call", trace=trace, parent=root
    ):
        pass
    tele.record_gauge("agent_platform_queue_backlog", 2.0, labels={"operation": "scheduler.claim"})
    tele.timing(
        "agent_platform_model_latency_seconds",
        1.0,
        labels={"model_id": "deepseek-chat", "outcome": "ok"},
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    by_name = {span.name: span for span in spans}
    assert by_name["run.created"].context.trace_id == int(trace.trace_id, 16)
    assert by_name["model.call"].context.trace_id == int(trace.trace_id, 16)
    assert by_name["model.call"].parent.span_id == by_name["run.created"].context.span_id
    # Histogram + gauge instruments exist and produced metrics.
    exported = metric_reader.get_metrics_data()
    names = {m.name for resource_metrics in exported.resource_metrics for m in resource_metrics.scope_metrics[0].metrics}
    assert "agent_platform_model_latency_seconds" in names
    assert "agent_platform_queue_backlog" in names


# ---------------------------------------------------------------------------
# Session provider wrapper (model.call RED)
# ---------------------------------------------------------------------------


def test_telemetry_session_provider_wraps_and_records() -> None:
    from enterprise_agent_platform.reference.session import InMemoryRunSessionProvider

    sink = InMemoryDiagnosticSink()
    tele = _telemetry(sink)
    inner = InMemoryRunSessionProvider()
    wrapper = maybe_wrap_sessions(
        inner, tele, model_id="reference"
    )
    assert isinstance(wrapper, TelemetrySessionProvider)

    async def _drive() -> None:
        handle = await wrapper.open(
            run_id="run_1", intent="test", resource_refs=(), host_context_ref=None
        )
        answer = await wrapper.followup(handle, "hello?", read_only=True)
        assert isinstance(answer, str)
        await wrapper.close(handle)

    asyncio.run(_drive())
    trace_events = [e for e in sink.events if e.channel == "trace"]
    assert any(e.name == "model.call" for e in trace_events)
    metrics = {e.name for e in sink.events if e.channel in {"metric", "histogram"}}
    assert "agent_platform_model_calls_total" in metrics
    assert "agent_platform_model_latency_seconds" in metrics


# ---------------------------------------------------------------------------
# FastAPI RED middleware + /metrics route
# ---------------------------------------------------------------------------


def test_api_red_middleware_records_metrics() -> None:
    from enterprise_agent_platform.reference.local_stack import REFERENCE_LOCAL_BEARER

    sink = InMemoryDiagnosticSink()
    container = create_container()
    from dataclasses import replace

    container = replace(container, telemetry=_telemetry(sink))
    app = create_app(container)
    client = TestClient(app)
    response = client.get(
        "/v1/runs/does-not-exist", headers={"Authorization": REFERENCE_LOCAL_BEARER}
    )
    assert response.status_code == 404
    trace_events = [e for e in sink.events if e.channel == "trace"]
    assert any(e.name == "http.request" for e in trace_events)
    metric_events = [e for e in sink.events if e.channel == "metric"]
    assert any(
        ("http.route", "/v1/runs/{run_id}") in e.labels
        and ("http.status_class", "4xx") in e.labels
        for e in metric_events
    )


def test_metrics_route_exposes_prometheus_text() -> None:
    from prometheus_client import CollectorRegistry

    registry = CollectorRegistry()
    tele = DiagnosticTelemetry(
        sink=PrometheusMetricSink(registry=registry),
        metric_labels=default_metric_label_policy(),
    )
    container = create_container()
    from dataclasses import replace

    container = replace(container, telemetry=tele)
    app = create_app(container)
    client = TestClient(app)
    response = client.get("/api/agent-platform/v1/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# Route buckets + JSON log correlation
# ---------------------------------------------------------------------------


def test_http_route_bucket_normalisation() -> None:
    assert http_route_bucket("/v1/runs") == "/v1/runs"
    assert http_route_bucket("/v1/runs/run_1") == "/v1/runs/{run_id}"
    assert http_route_bucket("/api/agent-platform/v1/runs/run_1/events/stream") == (
        "/v1/runs/{run_id}/events/stream"
    )
    assert http_route_bucket("/v1/runs/run_1/cancel") == "/v1/runs/{run_id}/cancel"
    assert http_route_bucket("/v1/chat") == "/v1/chat"
    assert http_route_bucket("/internal/v1/runtime/bootstrap") == "/internal/v1/runtime/*"
    assert http_route_bucket("/metrics") == "/metrics"
    assert http_status_class(200) == "2xx"
    assert http_status_class(500) == "5xx"


def test_json_log_formatter_correlation_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("telemetry_wiring.json")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    with correlation(trace_id="ab" * 16, run_id="run_corr", attempt_id="attempt_corr"):
        logger.info("hello")
    line = stream.getvalue().strip()
    assert '"trace_id":"abababababababababababababababab"' in line
    assert '"run_id":"run_corr"' in line
    assert '"attempt_id":"attempt_corr"' in line
    assert '"message":"hello"' in line