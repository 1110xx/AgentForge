"""Portable platform adapters for transport and lossy diagnostics."""
from .config_reader import (
    AppConfig,
    ConfigReader,
    FallbackConfig,
    LoggingConfig,
    ProviderConfig,
    ProviderParameters,
    ProviderType,
    SessionConfig,
)
from .message_bus import (
    InboxConsumer,
    InMemoryMessageBus,
    MessageBus,
    MessageDelivery,
    MessageEnvelope,
    NatsJetStreamBus,
)
from .outbox import OutboxPublishBatch, OutboxPublisher
from .provider_factory import (
    ProviderFactory,
    load_provider,
)
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
    "AppConfig",
    "CompositeDiagnosticSink",
    "ConfigReader",
    "CorrectnessSignal",
    "DiagnosticTelemetry",
    "FallbackConfig",
    "InMemoryDiagnosticSink",
    "InMemoryMessageBus",
    "InboxConsumer",
    "LoggingConfig",
    "MessageBus",
    "MessageDelivery",
    "MessageEnvelope",
    "MetricLabelPolicy",
    "NatsJetStreamBus",
    "OpenTelemetryDiagnosticSink",
    "OutboxPublishBatch",
    "OutboxPublisher",
    "PrometheusMetricSink",
    "ProviderConfig",
    "ProviderFactory",
    "ProviderParameters",
    "ProviderType",
    "SessionConfig",
    "SpanLink",
    "TelemetryPolicyError",
    "load_provider",
]
