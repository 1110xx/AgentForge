"""Lossy diagnostic signals with bounded traces and low-cardinality labels.

RunEvent/Checkpoint/EffectLedger and AuditEvent are persisted elsewhere. Nothing
in this module can commit, recover, approve, or mutate a business/security fact.

Phase 4.3 (G5) additions:
- Explicit trace context: ``SpanHandle`` carries a trace_id; ``begin_trace()``
  anchors a run-scoped trace; ``span(..., trace=..., parent=...)`` builds a
  parent/child tree. The OTLP sink starts the SDK span at *enter* time via
  ``Tracer.start_span`` (threading the trace_id through a parent context —
  root spans use a phantom parent so the trace id is preserved) and advertises
  the *assigned* span id in the ``SpanHandle``, so every Attempt-generation
  span shares one trace_id and Tempo can reconstruct the run waterfall.
- Histogram + gauge channels: ``record_histogram`` / ``record_gauge`` /
  ``timing`` for latency distributions (RED p50/p95/p99) and queue depth /
  concurrent pod gauges. Buckets are bounded per metric name.
"""
from __future__ import annotations

import math
import re
import secrets
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from types import MappingProxyType
from typing import Literal, Protocol


class TelemetryPolicyError(ValueError):
    """A signal would violate cardinality, privacy, or bounded-trace policy."""


class CorrectnessSignal(StrEnum):
    UNAUTHORIZED_WRITE = "unauthorized_write"
    DUPLICATE_EFFECT = "duplicate_effect"
    COMMITTED_CHECKPOINT_LOST = "committed_checkpoint_lost"
    ARTIFACT_CHECKSUM_MISMATCH = "artifact_checksum_mismatch"
    AUDIT_BYPASS = "audit_bypass"
    STALE_FENCE_WRITE = "stale_fence_write"
    CROSS_TENANT_ACCESS = "cross_tenant_access"
    MULTIPLE_ACTIVE_ATTEMPTS = "multiple_active_attempts"


LOW_CARDINALITY_LABEL_KEYS = frozenset({
    "workflow_class",
    "runtime_profile",
    "risk_class",
    "tool_name",
    "operation",
    "outcome",
    "failure_domain",
    "reason_class",
    "state",
    # Phase 4.3: business label keys (RED + run lifecycle + model cost)
    "status",
    "model_id",
    "http.route",
    "http.status_class",
})

BOUNDED_SPAN_NAMES = frozenset({
    "run.command.accept",
    "run.created",
    "run.terminal",
    "workflow.transition",
    "attempt.dispatch",
    "scheduler.claim",
    "job.submit",
    "pod.schedule",
    "lease.acquire",
    "lease.renew",
    "runtime.bootstrap",
    "model.call",
    "checkpoint.commit",
    "workspace_snapshot.write",
    "tool.execute",
    "effect.reconcile",
    "artifact.promote",
    "a2ui.validate",
    "ui_surface.commit",
    "sse.replay",
    "sse.ingest",
    "http.request",
    "nats.relay.publish",
    "log.ingest",
})

SPAN_ATTRIBUTE_KEYS = frozenset({
    "agent.platform.tenant.id",
    "agent.platform.run.id",
    "agent.platform.step.id",
    "agent.platform.execution_unit.id",
    "agent.platform.attempt.id",
    "agent.platform.lease.generation",
    "agent.platform.checkpoint.id",
    "agent.platform.tool_call.id",
    "agent.platform.approval.id",
    "agent.platform.effect.id",
    "agent.platform.artifact.id",
    "agent.platform.ui_surface.id",
    "agent.platform.event.id",
    "agent.platform.event.seq",
    "agent.platform.workflow.class",
    "agent.platform.runtime.profile",
    "agent.platform.risk.class",
    "agent.platform.tool.name",
    "agent.platform.outcome",
    "agent.platform.failure.domain",
    "agent.platform.reason.class",
    "agent.platform.state",
    # Phase 4.3: main-chain correlation attributes (SDD G.4)
    "agent.platform.model.id",
    "agent.platform.http.route",
    "agent.platform.http.status.class",
    "agent.platform.run.status",
    "agent.platform.attempt.status",
    "agent.platform.queue.depth",
    "agent.platform.worker.id",
    "exception.type",
})

# Bounded latency/dwell histograms: name → bucket boundaries (seconds).
BOUNDED_HISTOGRAMS: Mapping[str, tuple[float, ...]] = MappingProxyType({
    "agent_platform_http_latency_seconds": (
        0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
        30.0, 60.0, 120.0, 300.0,
    ),
    "agent_platform_run_latency_seconds": (
        5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0, 7200.0,
        14400.0, 28800.0,
    ),
    "agent_platform_queue_latency_seconds": (
        0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0,
    ),
    "agent_platform_model_latency_seconds": (
        0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0,
    ),
    "agent_platform_job_submit_seconds": (
        0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0,
    ),
    "agent_platform_pod_schedule_seconds": (
        1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0,
    ),
})

_TOKEN = re.compile(r"[a-z0-9/][a-z0-9._/*{}:-]{0,127}$")  # route buckets carry /{}*
_METRIC_NAME = re.compile(r"[a-z][a-z0-9_]{0,127}$")
_TRACE_ID = re.compile(r"[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"[0-9a-f]{16}$")


def _new_trace_id() -> str:
    return secrets.token_hex(16)  # 32 hex chars


def _new_span_id() -> str:
    return secrets.token_hex(8)  # 16 hex chars


@dataclass(frozen=True, slots=True)
class MetricLabelPolicy:
    registered_values: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        registry: dict[str, frozenset[str]] = {}
        for key, values in self.registered_values.items():
            if key not in LOW_CARDINALITY_LABEL_KEYS:
                raise TelemetryPolicyError(f"metric label key is not registered: {key}")
            normalized = frozenset(values)
            if not normalized or len(normalized) > 256:
                raise TelemetryPolicyError(f"metric label registry is unbounded: {key}")
            if any(_TOKEN.fullmatch(value) is None for value in normalized):
                raise TelemetryPolicyError(f"metric label value is invalid: {key}")
            registry[key] = normalized
        object.__setattr__(self, "registered_values", MappingProxyType(registry))

    def validate(self, labels: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
        normalized: list[tuple[str, str]] = []
        for key, value in labels.items():
            allowed = self.registered_values.get(key)
            if allowed is None:
                raise TelemetryPolicyError(f"metric label key is not registered: {key}")
            if value not in allowed:
                raise TelemetryPolicyError(f"metric label value is not registered: {key}")
            normalized.append((key, value))
        return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class SpanLink:
    trace_id: str
    span_id: str

    def __post_init__(self) -> None:
        if (
            _TRACE_ID.fullmatch(self.trace_id) is None
            or _SPAN_ID.fullmatch(self.span_id) is None
            or self.trace_id == "0" * 32
            or self.span_id == "0" * 16
        ):
            raise TelemetryPolicyError("trace link identity is invalid")


@dataclass(frozen=True, slots=True)
class SpanHandle:
    """One node in a trace: trace_id (32 hex) + span_id (16 hex).

    ``span_id`` is the id the sink will assign (OTLP) or a pre-generated id
    (in-memory sinks) — pass this handle to ``span(..., parent=...)`` to nest
    a child under the span it represents.
    """

    trace_id: str
    span_id: str

    def __post_init__(self) -> None:
        if (
            _TRACE_ID.fullmatch(self.trace_id) is None
            or _SPAN_ID.fullmatch(self.span_id) is None
            or self.trace_id == "0" * 32
            or self.span_id == "0" * 16
        ):
            raise TelemetryPolicyError("span handle identity is invalid")


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    channel: Literal["trace", "metric", "zero_tolerance", "histogram", "gauge"]
    name: str
    value: float | None = None
    attributes: tuple[tuple[str, str | int | float | bool], ...] = ()
    labels: tuple[tuple[str, str], ...] = ()
    links: tuple[SpanLink, ...] = ()
    started_at_ns: int | None = None
    ended_at_ns: int | None = None
    # Phase 4.3: explicit trace identity.
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None


class DiagnosticSink(Protocol):
    def emit(self, event: DiagnosticEvent) -> None: ...

    def start_span(
        self, event: DiagnosticEvent
    ) -> str | None:
        """Begin a trace span; return the assigned span id (None = keep event id)."""
        return None

    def end_span(self, event: DiagnosticEvent) -> None:
        """End the trace span identified by ``event.span_id``."""


class InMemoryDiagnosticSink:
    """Real, deterministic signal sink used by unit and in-process tests."""

    def __init__(self) -> None:
        self.events: list[DiagnosticEvent] = []
        self._active: dict[str, DiagnosticEvent] = {}

    def emit(self, event: DiagnosticEvent) -> None:
        if event.channel != "trace":
            self.events.append(event)

    def start_span(self, event: DiagnosticEvent) -> str | None:
        self._active[event.span_id or ""] = event
        return None

    def end_span(self, event: DiagnosticEvent) -> None:
        started = self._active.pop(event.span_id or "", None)
        if started is not None:
            from dataclasses import replace

            event = replace(
                event,
                started_at_ns=started.started_at_ns,
                trace_id=started.trace_id,
                parent_span_id=started.parent_span_id,
            )
        self.events.append(event)


def _span_attributes(
    attributes: Mapping[str, str | int | float | bool]
) -> tuple[tuple[str, str | int | float | bool], ...]:
    normalized: list[tuple[str, str | int | float | bool]] = []
    for key, value in attributes.items():
        if key not in SPAN_ATTRIBUTE_KEYS:
            raise TelemetryPolicyError(f"span attribute key is not registered: {key}")
        if isinstance(value, str):
            if not value or len(value) > 255:
                raise TelemetryPolicyError(f"span attribute value is invalid: {key}")
        elif not isinstance(value, (int, float, bool)) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            raise TelemetryPolicyError(f"span attribute value is invalid: {key}")
        normalized.append((key, value))
    return tuple(sorted(normalized))


class DiagnosticTelemetry:
    """Best-effort diagnostics; sink failures are observable only as a False return."""

    def __init__(self, *, sink: DiagnosticSink, metric_labels: MetricLabelPolicy) -> None:
        self._sink = sink
        self._metric_labels = metric_labels

    def begin_trace(self, trace_id: str | None = None) -> SpanHandle:
        """Anchor a shared trace (optionally reusing an inbound request trace_id)."""
        resolved = trace_id or _new_trace_id()
        if _TRACE_ID.fullmatch(resolved) is None:
            raise TelemetryPolicyError("span trace id is invalid")
        return SpanHandle(trace_id=resolved, span_id=_new_span_id())

    @contextmanager
    def span(
        self,
        name: str,
        *,
        attributes: Mapping[str, str | int | float | bool] | None = None,
        links: Sequence[SpanLink] = (),
        trace: SpanHandle | None = None,
        parent: SpanHandle | None = None,
    ) -> Iterator[SpanHandle]:
        if name not in BOUNDED_SPAN_NAMES:
            raise TelemetryPolicyError(f"span operation is not bounded: {name}")
        normalized_attributes = _span_attributes(attributes or {})
        normalized_links = tuple(links)
        if any(not isinstance(link, SpanLink) for link in normalized_links):
            raise TelemetryPolicyError("span link is invalid")
        if parent is not None and trace is not None and parent.trace_id != trace.trace_id:
            raise TelemetryPolicyError("span parent must share the span trace")
        trace_id = trace.trace_id if trace is not None else _new_trace_id()
        if _TRACE_ID.fullmatch(trace_id) is None:
            raise TelemetryPolicyError("span trace id is invalid")
        parent_span_id = parent.span_id if parent is not None else None
        pre_span_id = _new_span_id()
        started_at_ns = time.time_ns()
        start_event = DiagnosticEvent(
            channel="trace",
            name=name,
            attributes=normalized_attributes,
            links=normalized_links,
            started_at_ns=started_at_ns,
            trace_id=trace_id,
            span_id=pre_span_id,
            parent_span_id=parent_span_id,
        )
        assigned = self._start_span(start_event)
        handle = SpanHandle(trace_id=trace_id, span_id=assigned or pre_span_id)
        try:
            yield handle
        except BaseException as error:
            failed_attributes = dict(normalized_attributes)
            failed_attributes["exception.type"] = type(error).__name__
            end_event = DiagnosticEvent(
                channel="trace",
                name=name,
                attributes=_span_attributes(failed_attributes),
                links=normalized_links,
                started_at_ns=started_at_ns,
                ended_at_ns=time.time_ns(),
                trace_id=trace_id,
                span_id=handle.span_id,
                parent_span_id=parent_span_id,
            )
            self._end_span(end_event)
            raise
        else:
            end_event = DiagnosticEvent(
                channel="trace",
                name=name,
                attributes=normalized_attributes,
                links=normalized_links,
                started_at_ns=started_at_ns,
                ended_at_ns=time.time_ns(),
                trace_id=trace_id,
                span_id=handle.span_id,
                parent_span_id=parent_span_id,
            )
            self._end_span(end_event)

    def _start_span(self, event: DiagnosticEvent) -> str | None:
        try:
            return self._sink.start_span(event)
        except Exception:  # noqa: BLE001 - diagnostics must never gate business commits
            return None

    def _end_span(self, event: DiagnosticEvent) -> None:
        try:
            self._sink.end_span(event)
        except Exception:  # noqa: BLE001, S110 - diagnostics must never gate business commits
            pass

    def _emit(self, event: DiagnosticEvent) -> bool:
        try:
            self._sink.emit(event)
        except Exception:  # noqa: BLE001
            return False
        return True

    def record_metric(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str],
    ) -> bool:
        if _METRIC_NAME.fullmatch(name) is None or not math.isfinite(float(value)):
            raise TelemetryPolicyError("metric name or value is invalid")
        return self._emit(
            DiagnosticEvent(
                channel="metric",
                name=name,
                value=float(value),
                labels=self._metric_labels.validate(labels),
            )
        )

    def record_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str],
    ) -> bool:
        """Current-state gauge (queue depth, concurrent pods)."""
        if _METRIC_NAME.fullmatch(name) is None or not math.isfinite(float(value)):
            raise TelemetryPolicyError("gauge name or value is invalid")
        return self._emit(
            DiagnosticEvent(
                channel="gauge",
                name=name,
                value=float(value),
                labels=self._metric_labels.validate(labels),
            )
        )

    def record_histogram(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str],
    ) -> bool:
        """Latency/dwell observation into a bounded histogram (RED p50/p95/p99)."""
        if name not in BOUNDED_HISTOGRAMS or not math.isfinite(float(value)):
            raise TelemetryPolicyError("histogram name or value is invalid")
        return self._emit(
            DiagnosticEvent(
                channel="histogram",
                name=name,
                value=float(value),
                labels=self._metric_labels.validate(labels),
            )
        )

    def timing(
        self,
        name: str,
        duration_seconds: float,
        *,
        labels: Mapping[str, str],
    ) -> bool:
        return self.record_histogram(name, duration_seconds, labels=labels)

    def record_zero_tolerance(
        self,
        signal: CorrectnessSignal,
        attributes: Mapping[str, str | int | float | bool] | None = None,
    ) -> bool:
        if not isinstance(signal, CorrectnessSignal):
            raise TelemetryPolicyError("zero-tolerance signal is invalid")
        labels = self._metric_labels.validate({"reason_class": signal.value})
        return self._emit(
            DiagnosticEvent(
                channel="zero_tolerance",
                name="agent_platform_correctness_violation_total",
                value=1.0,
                attributes=_span_attributes(attributes or {}),
                labels=labels,
            )
        )


class CompositeDiagnosticSink:
    def __init__(self, sinks: Sequence[DiagnosticSink]) -> None:
        self._sinks = tuple(sinks)
        self.failed_export_types: list[str] = []

    def emit(self, event: DiagnosticEvent) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception as error:  # noqa: BLE001
                self.failed_export_types.append(type(error).__name__)

    def start_span(self, event: DiagnosticEvent) -> str | None:
        assigned: str | None = None
        for sink in self._sinks:
            try:
                result = sink.start_span(event)
            except Exception:  # noqa: BLE001, S112 - diagnostics never gate
                continue
            if result is not None and assigned is None:
                assigned = result
        return assigned

    def end_span(self, event: DiagnosticEvent) -> None:
        for sink in self._sinks:
            try:
                sink.end_span(event)
            except Exception:  # noqa: BLE001, S112 - diagnostics never gate
                continue


def _buckets_for(name: str) -> tuple[float, ...]:
    return BOUNDED_HISTOGRAMS[name]


class OpenTelemetryDiagnosticSink:
    def __init__(self, *, tracer: object, meter: object) -> None:
        self._tracer = tracer
        self._meter = meter
        self._counters: dict[tuple[str, tuple[str, ...]], object] = {}
        self._histograms: dict[tuple[str, tuple[str, ...]], object] = {}
        self._gauges: dict[tuple[str, tuple[str, ...]], object] = {}
        self._gauge_last: dict[tuple[str, tuple[str, ...]], float] = {}
        self._active_spans: dict[str, object] = {}
        self._lock = Lock()

    @classmethod
    def from_otlp_http(
        cls,
        *,
        service_name: str,
        endpoint: str,
        service_version: str,
        environment: str,
    ) -> OpenTelemetryDiagnosticSink:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("OTLP HTTP endpoint is invalid")
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": service_version,
                "deployment.environment.name": environment,
            }
        )
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
        )
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=f"{endpoint.rstrip('/')}/v1/metrics")
                ),
            ),
        )
        return cls(
            tracer=tracer_provider.get_tracer(service_name, service_version),
            meter=meter_provider.get_meter(service_name, service_version),
        )

    def start_span(self, event: DiagnosticEvent) -> str | None:
        if event.trace_id is None or event.span_id is None:
            return None
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            TraceFlags,
            set_span_in_context,
        )

        # Parent chain: every span's *parent* context carries the shared trace_id.
        # Roots get a phantom parent (span_id != the root's own id) so ``start_span``
        # derives the trace id from our context — the SDK still assigns the real
        # span id, which we advertise back so children can nest correctly.
        if event.parent_span_id is not None:
            parent_id = int(event.parent_span_id, 16)
        else:
            parent_id = int(_new_span_id(), 16)
        parent_context = SpanContext(
            trace_id=int(event.trace_id, 16),
            span_id=parent_id,
            is_remote=False,
            # Constructor form: plain int flags break the SDK sampling check.
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        context = set_span_in_context(NonRecordingSpan(parent_context))
        try:
            span = self._tracer.start_span(
                event.name,
                context=context,
                attributes=dict(event.attributes),
                start_time=event.started_at_ns,
            )
        except Exception:  # noqa: BLE001 - diagnostics must never crash the caller
            return None
        assigned = f"{span.get_span_context().span_id:016x}"
        with self._lock:
            self._active_spans[assigned] = span
        return assigned

    def end_span(self, event: DiagnosticEvent) -> None:
        if event.span_id is None:
            return
        with self._lock:
            span = self._active_spans.pop(event.span_id, None)
        if span is None:
            return
        exception_type = next(
            (value for key, value in event.attributes if key == "exception.type"), None
        )
        if exception_type is not None:
            span.set_attribute("exception.type", str(exception_type))
        span.end(end_time=event.ended_at_ns)

    def emit(self, event: DiagnosticEvent) -> None:
        if event.channel == "trace":
            return  # span lifecycle is start_span/end_span

        if event.channel == "histogram":
            label_names = tuple(key for key, value in event.labels)
            key = (event.name, label_names)
            with self._lock:
                instrument = self._histograms.get(key)
                if instrument is None:
                    # 1.29–1.44 pin range: newer SDKs renamed the boundaries
                    # parameter to advisory-only; older SDKs take explicit ones.
                    try:
                        instrument = self._meter.create_histogram(
                            event.name,
                            description="Bounded latency/dwell histogram",
                            explicit_bucket_boundaries_advisory=list(
                                _buckets_for(event.name)
                            ),
                        )
                    except TypeError:
                        instrument = self._meter.create_histogram(
                            event.name,
                            description="Bounded latency/dwell histogram",
                            explicit_bucket_boundaries=list(_buckets_for(event.name)),
                        )
                    self._histograms[key] = instrument
            instrument.record(event.value or 0.0, attributes=dict(event.labels))
            return
        if event.channel == "gauge":
            label_names = tuple(key for key, value in event.labels)
            key = (event.name, label_names)
            with self._lock:
                instrument = self._gauges.get(key)
                if instrument is None:
                    instrument = self._meter.create_up_down_counter(
                        event.name,
                        description="Current-state gauge (up-down delta)",
                    )
                    self._gauges[key] = instrument
                previous = self._gauge_last.get(key, 0.0)
                delta = (event.value or 0.0) - previous
                self._gauge_last[key] = event.value or 0.0
            instrument.add(delta, attributes=dict(event.labels))
            return
        self._emit_counter(event)

    def _emit_counter(self, event: DiagnosticEvent) -> None:
        label_names = tuple(key for key, value in event.labels)
        key = (event.name, label_names)
        with self._lock:
            counter = self._counters.get(key)
            if counter is None:
                counter = self._meter.create_counter(event.name)
                self._counters[key] = counter
        counter.add(event.value or 0.0, attributes=dict(event.labels))


class PrometheusMetricSink:
    """Exports counter / gauge / histogram into one local Prometheus registry."""

    def __init__(self, *, registry: object | None = None) -> None:
        from prometheus_client import REGISTRY

        self._registry = registry or REGISTRY
        self._counters: dict[tuple[str, tuple[str, ...]], object] = {}
        self._gauges: dict[tuple[str, tuple[str, ...]], object] = {}
        self._histograms: dict[tuple[str, tuple[str, ...]], object] = {}
        self._lock = Lock()

    @property
    def registry(self) -> object:
        return self._registry

    def start_span(self, event: DiagnosticEvent) -> str | None:
        return None

    def end_span(self, event: DiagnosticEvent) -> None:
        return

    def emit(self, event: DiagnosticEvent) -> None:
        if event.channel == "trace":
            return
        from prometheus_client import Counter, Gauge, Histogram

        label_names = tuple(key for key, value in event.labels)
        key = (event.name, label_names)
        metric_name = event.name.removesuffix("_total")
        with self._lock:
            if event.channel == "gauge":
                instrument = self._gauges.get(key)
                if instrument is None:
                    instrument = Gauge(
                        metric_name,
                        "Enterprise Agent Platform bounded current-state gauge",
                        labelnames=label_names,
                        registry=self._registry,
                    )
                    self._gauges[key] = instrument
                instrument.labels(**dict(event.labels)).set(event.value or 0.0)
                return
            if event.channel == "histogram":
                instrument = self._histograms.get(key)
                if instrument is None:
                    instrument = Histogram(
                        metric_name,
                        "Enterprise Agent Platform bounded latency histogram",
                        labelnames=label_names,
                        buckets=list(_buckets_for(event.name)),
                        registry=self._registry,
                    )
                    self._histograms[key] = instrument
                instrument.labels(**dict(event.labels)).observe(event.value or 0.0)
                return
            instrument = self._counters.get(key)
            if instrument is None:
                instrument = Counter(
                    metric_name,
                    "Enterprise Agent Platform bounded diagnostic counter",
                    labelnames=label_names,
                    registry=self._registry,
                )
                self._counters[key] = instrument
        instrument.labels(**dict(event.labels)).inc(event.value or 0.0)


def prometheus_text(sink: object) -> str:
    """Render the Prometheus exposition text for a prometheus-backed sink."""
    from prometheus_client import generate_latest

    registry = getattr(sink, "registry", None)
    if registry is None:
        return ""
    return generate_latest(registry).decode()


__all__ = [
    "BOUNDED_HISTOGRAMS",
    "BOUNDED_SPAN_NAMES",
    "LOW_CARDINALITY_LABEL_KEYS",
    "CompositeDiagnosticSink",
    "CorrectnessSignal",
    "DiagnosticEvent",
    "DiagnosticSink",
    "DiagnosticTelemetry",
    "InMemoryDiagnosticSink",
    "MetricLabelPolicy",
    "OpenTelemetryDiagnosticSink",
    "PrometheusMetricSink",
    "SpanHandle",
    "SpanLink",
    "TelemetryPolicyError",
    "prometheus_text",
]