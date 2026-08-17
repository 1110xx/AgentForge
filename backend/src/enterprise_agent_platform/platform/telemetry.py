"""Lossy diagnostic signals with bounded traces and low-cardinality labels.

RunEvent/Checkpoint/EffectLedger and AuditEvent are persisted elsewhere. Nothing
in this module can commit, recover, approve, or mutate a business/security fact.
"""
from __future__ import annotations

import math
import re
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
})

BOUNDED_SPAN_NAMES = frozenset({
    "run.command.accept",
    "workflow.transition",
    "attempt.dispatch",
    "lease.acquire",
    "runtime.bootstrap",
    "checkpoint.commit",
    "workspace_snapshot.write",
    "tool.execute",
    "effect.reconcile",
    "artifact.promote",
    "a2ui.validate",
    "ui_surface.commit",
    "sse.replay",
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
    "exception.type",
})

_TOKEN = re.compile(r"[a-z0-9][a-z0-9.-]{0,127}$")
_METRIC_NAME = re.compile(r"[a-z][a-z0-9_]{0,127}$")
_TRACE_ID = re.compile(r"[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"[0-9a-f]{16}$")


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
class DiagnosticEvent:
    channel: Literal["trace", "metric", "zero_tolerance"]
    name: str
    value: float | None = None
    attributes: tuple[tuple[str, str | int | float | bool], ...] = ()
    labels: tuple[tuple[str, str], ...] = ()
    links: tuple[SpanLink, ...] = ()
    started_at_ns: int | None = None
    ended_at_ns: int | None = None


class DiagnosticSink(Protocol):
    def emit(self, event: DiagnosticEvent) -> None: ...


class InMemoryDiagnosticSink:
    """Real, deterministic signal sink used by unit and in-process integration tests."""

    def __init__(self) -> None:
        self.events: list[DiagnosticEvent] = []

    def emit(self, event: DiagnosticEvent) -> None:
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

    def _emit(self, event: DiagnosticEvent) -> bool:
        try:
            self._sink.emit(event)
        except Exception:  # noqa: BLE001 - diagnostics must never gate business commits
            return False
        return True

    @contextmanager
    def span(
        self,
        name: str,
        *,
        attributes: Mapping[str, str | int | float | bool] | None = None,
        links: Sequence[SpanLink] = (),
    ) -> Iterator[None]:
        if name not in BOUNDED_SPAN_NAMES:
            raise TelemetryPolicyError(f"span operation is not bounded: {name}")
        normalized_attributes = _span_attributes(attributes or {})
        normalized_links = tuple(links)
        if any(not isinstance(link, SpanLink) for link in normalized_links):
            raise TelemetryPolicyError("span link is invalid")
        started_at_ns = time.time_ns()
        try:
            yield
        except BaseException as error:
            failed_attributes = dict(normalized_attributes)
            failed_attributes["exception.type"] = type(error).__name__
            self._emit(
                DiagnosticEvent(
                    channel="trace",
                    name=name,
                    attributes=_span_attributes(failed_attributes),
                    links=normalized_links,
                    started_at_ns=started_at_ns,
                    ended_at_ns=time.time_ns(),
                )
            )
            raise
        else:
            self._emit(
                DiagnosticEvent(
                    channel="trace",
                    name=name,
                    attributes=normalized_attributes,
                    links=normalized_links,
                    started_at_ns=started_at_ns,
                    ended_at_ns=time.time_ns(),
                )
            )

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


class OpenTelemetryDiagnosticSink:
    def __init__(self, *, tracer: object, meter: object) -> None:
        self._tracer = tracer
        self._meter = meter
        self._counters: dict[tuple[str, tuple[str, ...]], object] = {}
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

    def emit(self, event: DiagnosticEvent) -> None:
        if event.channel == "trace":
            from opentelemetry.trace import Link, SpanContext, TraceFlags

            links = [
                Link(
                    SpanContext(
                        trace_id=int(link.trace_id, 16),
                        span_id=int(link.span_id, 16),
                        is_remote=True,
                        trace_flags=TraceFlags.SAMPLED,
                    )
                )
                for link in event.links
            ]
            span = self._tracer.start_span(
                event.name,
                links=links,
                start_time=event.started_at_ns,
                attributes=dict(event.attributes),
            )
            span.end(end_time=event.ended_at_ns)
            return
        label_names = tuple(key for key, value in event.labels)
        key = (event.name, label_names)
        with self._lock:
            counter = self._counters.get(key)
            if counter is None:
                counter = self._meter.create_counter(event.name)
                self._counters[key] = counter
        counter.add(event.value or 0.0, attributes=dict(event.labels))


class PrometheusMetricSink:
    def __init__(self, *, registry: object | None = None) -> None:
        from prometheus_client import REGISTRY

        self._registry = registry or REGISTRY
        self._counters: dict[tuple[str, tuple[str, ...]], object] = {}
        self._lock = Lock()

    def emit(self, event: DiagnosticEvent) -> None:
        if event.channel == "trace":
            return
        from prometheus_client import Counter

        label_names = tuple(key for key, value in event.labels)
        key = (event.name, label_names)
        with self._lock:
            counter = self._counters.get(key)
            if counter is None:
                metric_name = event.name.removesuffix("_total")
                counter = Counter(
                    metric_name,
                    "Enterprise Agent Platform bounded diagnostic counter",
                    labelnames=label_names,
                    registry=self._registry,
                )
                self._counters[key] = counter
        counter.labels(**dict(event.labels)).inc(event.value or 0.0)


__all__ = [
    "BOUNDED_SPAN_NAMES",
    "LOW_CARDINALITY_LABEL_KEYS",
    "CompositeDiagnosticSink",
    "CorrectnessSignal",
    "DiagnosticEvent",
    "DiagnosticTelemetry",
    "InMemoryDiagnosticSink",
    "MetricLabelPolicy",
    "OpenTelemetryDiagnosticSink",
    "PrometheusMetricSink",
    "SpanLink",
    "TelemetryPolicyError",
]
