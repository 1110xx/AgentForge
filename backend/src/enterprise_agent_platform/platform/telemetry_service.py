"""Phase 4.3 (G5) telemetry assembly — env-gated, backwards-compatible.

``create_telemetry_from_env()`` returns ``None`` when no OTLP endpoint and no
Prometheus export are configured, so every existing local/test path keeps
running with zero signal cost. When configured, one ``DiagnosticTelemetry``
instance drives a composite sink (OTLP HTTP + Prometheus registry), and the
shared objects live on ``AgentPlatformContainer.telemetry``.

Also hosts the run-scoped ``TelemetrySessionProvider`` wrapper (model.call
spans + RED-by-model metrics) and the bounded route-bucket mapping used by the
FastAPI RED middleware.
"""
from __future__ import annotations

import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from enterprise_agent_platform.execution.session import (
    RunSessionProvider,
    SessionHandle,
)
from enterprise_agent_platform.platform.telemetry import (
    CompositeDiagnosticSink,
    DiagnosticTelemetry,
    InMemoryDiagnosticSink,
    MetricLabelPolicy,
    OpenTelemetryDiagnosticSink,
    PrometheusMetricSink,
    SpanHandle,
)

# ---------------------------------------------------------------------------
# Bounded metric label registries (SDD G.4 business metric groups)
# ---------------------------------------------------------------------------

# In-flight HTTP request span (set by the FastAPI RED middleware) so the
# run-created span nests under the API request that triggered it.
_request_span: ContextVar[SpanHandle | None] = ContextVar(
    "request_span", default=None
)


@contextmanager
def request_span_context(handle: SpanHandle | None) -> Iterator[None]:
    token = _request_span.set(handle)
    try:
        yield
    finally:
        _request_span.reset(token)


def current_request_span() -> SpanHandle | None:
    return _request_span.get()


_METRIC_LABEL_REGISTRIES: Mapping[str, frozenset[str]] = {
    # run lifecycle / terminal counters
    "status": frozenset({
        "queued",
        "running",
        "succeeded",
        "failed",
        "timeout",
        "cancelled",
        "recovering",
        "waiting_approval",
    }),
    "state": frozenset({
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "recovering",
    }),
    "operation": frozenset({
        "run.created",
        "run.terminal",
        "attempt.reserved",
        "attempt.claimed",
        "attempt.succeeded",
        "attempt.failed",
        "scheduler.claim",
        "model.call",
        "checkpoint.commit",
        "lease.renew",
        "lease.acquire",
        "job.submit",
        "nats.relay.publish",
        "effect.reconcile",
        "artifact.promote",
        "ui_surface.commit",
        "sse.replay",
    }), 
    "outcome": frozenset({"ok", "error", "retry", "cancel", "redirect"}),
    # model cost / latency (RED by model)
    "model_id": frozenset({
        "deepseek-chat",
        "deepseek-reasoner",
        "gpt-4o",
        "gpt-4-turbo",
        "gpt-3.5-turbo",
        "claude-3-5-sonnet-20241022",
        "claude-3-opus-20240229",
        "reference",
        "unknown",
    }),
    # API RED
    "http.route": frozenset({
        "/v1/runs",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/events",
        "/v1/runs/{run_id}/events/stream",
        "/v1/runs/{run_id}/cancel",
        "/v1/runs/{run_id}/reruns",
        "/v1/runs/{run_id}/followups",
        "/v1/runs/{run_id}/attempts",
        "/v1/runs/{run_id}/actions",
        "/v1/runs/{run_id}/effects",
        "/v1/runs/{run_id}/surfaces",
        "/v1/runs/{run_id}/artifacts",
        "/v1/chat",
        "/internal/v1/runtime/*",
        "/health/live",
        "/health/ready",
        "/metrics",
        "other",
    }),
    "http.status_class": frozenset({"2xx", "3xx", "4xx", "5xx"}),
    "reason_class": frozenset({
        "unauthorized_write",
        "duplicate_effect",
        "committed_checkpoint_lost",
        "artifact_checksum_mismatch",
        "audit_bypass",
        "stale_fence_write",
        "cross_tenant_access",
        "multiple_active_attempts",
        "stale_generation",
        "lease_expired",
        "checkpoint_conflict",
        "model_error",
        "timeout",
        "store_unavailable",
    }),
    "failure_domain": frozenset({
        "runtime",
        "model",
        "lease",
        "checkpoint",
        "store",
        "pipeline",
        "network",
        "workspace",
    }),
    "workflow_class": frozenset({"business-analysis", "reference", "default"}),
    "runtime_profile": frozenset({"business-analysis", "reference"}),
    "tool_name": frozenset({
        "file_read",
        "file_write",
        "bash",
        "remote_read_tool",
        "remote_model_call",
        "remote_publish_artifact",
        "remote_propose_action",
    }),
    "risk_class": frozenset({"low", "medium", "high"}),
}

def default_metric_label_policy() -> MetricLabelPolicy:
    """Policy with the standard Phase 4.3 bounded label registries."""
    return MetricLabelPolicy(registered_values=_METRIC_LABEL_REGISTRIES)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _env_bool(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes", "on"}


def create_telemetry_from_env(
    *,
    service_name: str | None = None,
    service_version: str | None = None,
    environment: str | None = None,
) -> DiagnosticTelemetry | None:
    """Build the platform telemetry from ``AGENT_PLATFORM_*`` env contract.

    Env contract (aligned with deploy/observability + helm values):

    * ``AGENT_PLATFORM_OTLP_ENDPOINT``   — OTLP/HTTP base URL of the collector
      (e.g. ``http://agent-platform-otel-collector:4318``). When set, spans and
      metrics stream to the collector over OTLP HTTP.
    * ``AGENT_PLATFORM_PROMETHEUS_ENABLED`` — ``1`` exposes a local Prometheus
      registry (pulled by the collector prometheus exporter or scraped via the
      API /metrics route).
    * ``AGENT_PLATFORM_SERVICE_NAME``    — resource ``service.name`` (default
      ``enterprise-agent-platform``).
    * ``AGENT_PLATFORM_SERVICE_VERSION`` — resource ``service.version``.
    * ``AGENT_PLATFORM_ENVIRONMENT``     — resource ``deployment.environment.name``.

    Returns ``None`` when nothing is configured (local/dev zero-cost default).
    """
    otlp_endpoint = _env("AGENT_PLATFORM_OTLP_ENDPOINT")
    prometheus_enabled = _env_bool("AGENT_PLATFORM_PROMETHEUS_ENABLED")
    if not otlp_endpoint and not prometheus_enabled:
        return None

    sinks: list[Any] = [InMemoryDiagnosticSink()]
    if otlp_endpoint:
        sinks.append(
            OpenTelemetryDiagnosticSink.from_otlp_http(
                service_name=_env("AGENT_PLATFORM_SERVICE_NAME", "enterprise-agent-platform"),
                endpoint=otlp_endpoint,
                service_version=_env("AGENT_PLATFORM_SERVICE_VERSION", "0.1.0"),
                environment=_env("AGENT_PLATFORM_ENVIRONMENT", "local"),
            )
        )
    if prometheus_enabled:
        from prometheus_client import CollectorRegistry

        sinks.append(PrometheusMetricSink(registry=CollectorRegistry()))
    return DiagnosticTelemetry(
        sink=CompositeDiagnosticSink(sinks),
        metric_labels=default_metric_label_policy(),
    )


def prometheus_registry(telemetry: DiagnosticTelemetry | None) -> object | None:
    """First Prometheus registry among the composite sinks (or ``None``)."""
    if telemetry is None:
        return None
    sink = getattr(telemetry, "_sink", None)
    if isinstance(sink, CompositeDiagnosticSink):
        for inner in sink._sinks:  # composite member list is stable within this package
            registry = getattr(inner, "registry", None)
            if registry is not None:
                return registry
        return None
    if isinstance(sink, PrometheusMetricSink):
        return sink.registry
    return None


def prometheus_enabled(telemetry: DiagnosticTelemetry | None) -> bool:
    return prometheus_registry(telemetry) is not None


def http_route_bucket(path: str) -> str:
    """Map an API path to one bounded route label (SDD G.4 API RED)."""
    # Host-embedded deployments mount the router under /api/agent-platform;
    # normalise so both spellings collapse to the same bounded bucket.
    if path.startswith("/api/agent-platform"):
        path = path[len("/api/agent-platform"):] or "/"
    if path == "/" or path in {"/health/live", "/health/ready"}:
        return path if path != "/" else "other"
    if path.startswith("/v1/runs"):
        rest = path[len("/v1/runs"):]
        if rest == "":
            return "/v1/runs"
        if not rest.startswith("/"):
            return "other"
        segments = rest.strip("/").split("/")
        sub = segments[1] if len(segments) > 1 else ""
        tail = segments[2:] if len(segments) > 2 else []
        if sub == "":
            return "/v1/runs/{run_id}"
        if sub == "events":
            return "/v1/runs/{run_id}/events/stream" if tail and tail[0] == "stream" \
                else "/v1/runs/{run_id}/events"
        if sub in {"cancel", "reruns", "followups", "attempts", "actions", "effects",
                   "surfaces", "artifacts"}:
            return f"/v1/runs/{{run_id}}/{sub}"
        return "other"
    if path.startswith("/internal/v1/"):
        return "/internal/v1/runtime/*"
    if path == "/api/agent-platform/v1/metrics" or path == "/metrics":
        return "/metrics"
    if path == "/api/agent-platform/v1/chat" or path == "/v1/chat":
        return "/v1/chat"
    return "other"


def http_status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


# ---------------------------------------------------------------------------
# Run-scoped model provider wrapper (SDD G.4: 模型调用最大单点)
# ---------------------------------------------------------------------------


class TelemetrySessionProvider:
    """Wraps a ``RunSessionProvider`` with ``model.call`` spans + RED metrics.

    Records per-call latency into ``agent_platform_model_latency_seconds`` and a
    ``agent_platform_model_calls_total{model_id, outcome}`` counter. Failures
    also increment ``agent_platform_model_errors_total``. The wrapper is
    transparent to callers (same protocol surface).
    """

    def __init__(
        self,
        inner: RunSessionProvider,
        *,
        telemetry: DiagnosticTelemetry,
        model_id: str = "unknown",
    ) -> None:
        self._inner = inner
        self._telemetry = telemetry
        self._model_id = model_id

    def __repr__(self) -> str:
        return f"TelemetrySessionProvider({self._inner!r})"

    async def open(
        self,
        *,
        run_id: str,
        intent: str,
        resource_refs: tuple[str, ...],
        host_context_ref: str | None,
    ) -> SessionHandle:
        started = time.perf_counter()
        try:
            handle = await self._inner.open(
                run_id=run_id,
                intent=intent,
                resource_refs=resource_refs,
                host_context_ref=host_context_ref,
            )
        except BaseException:
            self._record(started, "error", run_id=run_id)
            raise
        self._record(started, "ok", run_id=run_id)
        return handle

    async def run_task(self, handle: SessionHandle) -> None:
        started = time.perf_counter()
        try:
            await self._inner.run_task(handle)
        except BaseException:
            self._record(started, "error", run_id=handle.run_id)
            raise
        self._record(started, "ok", run_id=handle.run_id)

    async def followup(
        self,
        handle: SessionHandle,
        message: str,
        *,
        read_only: bool = True,
    ) -> str:
        started = time.perf_counter()
        try:
            answer = await self._inner.followup(
                handle, message, read_only=read_only
            )
        except BaseException:
            self._record(started, "error", run_id=handle.run_id)
            raise
        self._record(started, "ok", run_id=handle.run_id)
        return answer

    async def close(self, handle: SessionHandle) -> None:
        started = time.perf_counter()
        try:
            await self._inner.close(handle)
        except BaseException:
            self._record(started, "error", run_id=handle.run_id)
            raise
        self._record(started, "ok", run_id=handle.run_id)

    def _record(self, started: float, outcome: str, *, run_id: str) -> None:
        duration = time.perf_counter() - started
        tele = self._telemetry
        model = self._model_id
        trace = tele.begin_trace()
        tele.record_metric(
            "agent_platform_model_calls_total",
            1.0,
            labels={"model_id": model, "outcome": outcome},
        )
        if outcome == "error":
            tele.record_metric(
                "agent_platform_model_errors_total",
                1.0,
                labels={"model_id": model, "failure_domain": "model"},
            )
        tele.timing(
            "agent_platform_model_latency_seconds",
            duration,
            labels={"model_id": model, "outcome": outcome},
        )
        with tele.span(
            "model.call",
            attributes={
                "agent.platform.model.id": model,
                "agent.platform.run.id": run_id,
                "agent.platform.outcome": outcome,
            },
            trace=trace,
        ):
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def maybe_wrap_sessions(
    sessions: RunSessionProvider,
    telemetry: DiagnosticTelemetry | None,
    *,
    model_id: str | None = None,
) -> RunSessionProvider:
    """Wrap the model provider when telemetry is configured (pass-through else)."""
    if telemetry is None:
        return sessions
    return TelemetrySessionProvider(
        sessions,
        telemetry=telemetry,
        model_id=model_id or _env("AGENT_PLATFORM_MODEL_ID", "unknown"),
    )


__all__ = [
    "TelemetrySessionProvider",
    "create_telemetry_from_env",
    "current_request_span",
    "default_metric_label_policy",
    "http_route_bucket",
    "http_status_class",
    "maybe_wrap_sessions",
    "prometheus_enabled",
    "prometheus_registry",
    "request_span_context",
]