"""Portable platform adapters for transport and lossy diagnostics."""
from .message_bus import (
    InboxConsumer,
    InMemoryMessageBus,
    MessageBus,
    MessageDelivery,
    MessageEnvelope,
    NatsJetStreamBus,
)
from .outbox import OutboxPublishBatch, OutboxPublisher
from .telemetry import (
    CompositeDiagnosticSink,
    CorrectnessSignal,
    DiagnosticTelemetry,
    InMemoryDiagnosticSink,
    MetricLabelPolicy,
    OpenTelemetryDiagnosticSink,
    PrometheusMetricSink,
    SpanLink,
    TelemetryPolicyError,
)

__all__ = [
    "CompositeDiagnosticSink",
    "CorrectnessSignal",
    "DiagnosticTelemetry",
    "InMemoryDiagnosticSink",
    "InMemoryMessageBus",
    "InboxConsumer",
    "MessageBus",
    "MessageDelivery",
    "MessageEnvelope",
    "MetricLabelPolicy",
    "NatsJetStreamBus",
    "OpenTelemetryDiagnosticSink",
    "OutboxPublishBatch",
    "OutboxPublisher",
    "PrometheusMetricSink",
    "SpanLink",
    "TelemetryPolicyError",
]
