"""Versioned wake-up transport with transactional PostgreSQL Inbox semantics.

The bus transports stable references. It is never a source of Run, approval,
effect, audit, checkpoint, or artifact truth.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Protocol

from enterprise_agent_platform.domain.records import InboxMessageRecord
from enterprise_agent_platform.persistence.protocol import (
    PlatformError,
    PlatformStore,
    PlatformTransaction,
)

MESSAGE_FIELDS = frozenset({
    "message_id",
    "tenant_id",
    "topic",
    "schema_version",
    "payload_schema",
    "references",
    "causation_event_id",
    "traceparent",
    "tracestate",
})

REFERENCE_FIELDS = frozenset({
    "run_id",
    "step_id",
    "execution_unit_id",
    "attempt_id",
    "lease_id",
    "checkpoint_id",
    "source_checkpoint_id",
    "workspace_snapshot_id",
    "artifact_id",
    "artifact_version",
    "action_ref",
    "approval_id",
    "effect_id",
    "surface_id",
    "surface_revision",
    "event_id",
    "event_seq",
    "generation",
    "aggregate_id",
    "aggregate_version",
})

# Platform identifiers (run_<id>, unit_<id>, outbox_<id>, ...) include
# underscores; NATS accepts them in subjects, consumer and stream names.
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,254}$")
# NATS stream names allow letters, digits, underscores, dots, dashes
# (max 32 chars on the server). The deployment contract uses e.g.
# AGENT_PLATFORM, which the narrower identifier pattern would reject.
_STREAM = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$")
_TOPIC = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$")
_SCHEMA = re.compile(r"[a-z][a-z0-9._-]*/v[0-9]+(?:\.[0-9]+){0,2}$")
_TRACEPARENT = re.compile(
    r"(?P<version>[0-9a-f]{2})-(?P<trace>[0-9a-f]{32})-"
    r"(?P<span>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
_TRACESTATE_MEMBER = re.compile(
    r"[a-z0-9][_0-9a-z+\-*/]*(?:@[a-z0-9][_0-9a-z+\-*/]+)?=[\x20-\x2b\x2d-\x3c\x3e-\x7e]+$"
)


def _required_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a stable identifier")


def _validate_trace_context(traceparent: str | None, tracestate: str | None) -> None:
    if traceparent is None:
        if tracestate is not None:
            raise ValueError("tracestate requires traceparent")
        return
    match = _TRACEPARENT.fullmatch(traceparent)
    if (
        match is None
        or match.group("version") == "ff"
        or match.group("trace") == "0" * 32
        or match.group("span") == "0" * 16
    ):
        raise ValueError("traceparent is invalid")
    if tracestate is None:
        return
    members = [member.strip() for member in tracestate.split(",")]
    keys = [member.partition("=")[0] for member in members]
    if (
        len(tracestate) > 512
        or not 1 <= len(members) <= 32
        or len(keys) != len(set(keys))
        or any(_TRACESTATE_MEMBER.fullmatch(member) is None for member in members)
    ):
        raise ValueError("tracestate is invalid")


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    """Canonical transport envelope containing only stable opaque references."""
    message_id: str
    tenant_id: str
    topic: str
    schema_version: str
    payload_schema: str
    references: Mapping[str, str | int]
    causation_event_id: str | None = None
    traceparent: str | None = None
    tracestate: str | None = None

    def __post_init__(self) -> None:
        _required_identifier("message_id", self.message_id)
        _required_identifier("tenant_id", self.tenant_id)
        if not _TOPIC.fullmatch(self.topic):
            raise ValueError("topic is invalid")
        if self.schema_version != "platform-message/v1":
            raise ValueError("message schema version is unsupported")
        if not _SCHEMA.fullmatch(self.payload_schema):
            raise ValueError("payload schema version is invalid")
        if not isinstance(self.references, Mapping) or not self.references:
            raise ValueError("message requires stable references")
        normalized: dict[str, str | int] = {}
        for key, value in self.references.items():
            if key not in REFERENCE_FIELDS:
                raise ValueError(f"{key} is not a stable reference field")
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise TypeError(f"{key} is not a stable reference value")
            if isinstance(value, str):
                _required_identifier(key, value)
                if "://" in value:
                    raise ValueError(f"{key} is not a stable reference value")
            normalized[key] = value
        if self.causation_event_id is not None:
            _required_identifier("causation_event_id", self.causation_event_id)
        _validate_trace_context(self.traceparent, self.tracestate)
        object.__setattr__(self, "references", MappingProxyType(dict(sorted(normalized.items()))))

    def to_json_bytes(self) -> bytes:
        document = {
            "message_id": self.message_id,
            "tenant_id": self.tenant_id,
            "topic": self.topic,
            "schema_version": self.schema_version,
            "payload_schema": self.payload_schema,
            "references": dict(self.references),
            "causation_event_id": self.causation_event_id,
            "traceparent": self.traceparent,
            "tracestate": self.tracestate,
        }
        return json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> MessageEnvelope:
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("message is not valid JSON") from error
        if not isinstance(document, dict):
            raise TypeError("message must be a JSON object")
        unknown = set(document) - MESSAGE_FIELDS
        missing = MESSAGE_FIELDS - set(document)
        if unknown:
            raise ValueError(f"unknown message fields: {sorted(unknown)}")
        if missing:
            raise ValueError(f"missing message fields: {sorted(missing)}")
        return cls(**document)

    def payload_digest(self) -> str:
        return f"sha256:{hashlib.sha256(self.to_json_bytes()).hexdigest()}"


class MessageDelivery:
    """One explicit-ack delivery. ACK/NAK may be invoked exactly once."""

    def __init__(
        self,
        envelope: MessageEnvelope,
        delivery_count: int,
        ack: Callable[[], Awaitable[None]],
        nak: Callable[[], Awaitable[None]],
    ) -> None:
        self.envelope = envelope
        self.delivery_count = delivery_count
        self._ack = ack
        self._nak = nak
        self._settled = False

    async def ack(self) -> None:
        if self._settled:
            raise RuntimeError("message delivery is already settled")
        self._settled = True
        await self._ack()

    async def nak(self) -> None:
        if self._settled:
            raise RuntimeError("message delivery is already settled")
        self._settled = True
        await self._nak()


class MessageBus(Protocol):
    async def publish(self, envelope: MessageEnvelope) -> None: ...
    async def pull(
        self, topic: str, *, consumer: str, timeout: float
    ) -> MessageDelivery | None: ...
    async def close(self) -> None: ...


@dataclass(slots=True)
class _QueuedMessage:
    envelope: MessageEnvelope
    delivery_count: int = 1


class InMemoryMessageBus:
    """Explicit-ack transport test adapter; all durable truth remains in the Store."""

    def __init__(self) -> None:
        self._messages: dict[str, deque[_QueuedMessage]] = defaultdict(deque)
        self._condition = asyncio.Condition()
        self._closed = False

    async def publish(self, envelope: MessageEnvelope) -> None:
        async with self._condition:
            if self._closed:
                raise RuntimeError("message bus is closed")
            self._messages[envelope.topic].append(_QueuedMessage(envelope))
            self._condition.notify_all()

    async def _take(self, topic: str) -> _QueuedMessage:
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._closed or bool(self._messages.get(topic))
            )
            if self._closed:
                raise RuntimeError("message bus is closed")
            return self._messages[topic].popleft()

    async def pull(self, topic: str, *, consumer: str, timeout: float) -> MessageDelivery | None:
        del consumer
        if timeout <= 0:
            raise ValueError("pull timeout must be positive")
        try:
            queued = await asyncio.wait_for(self._take(topic), timeout)
        except TimeoutError:
            return None

        async def ack() -> None:
            return None

        async def nak() -> None:
            async with self._condition:
                self._messages[topic].appendleft(
                    _QueuedMessage(queued.envelope, queued.delivery_count + 1)
                )
                self._condition.notify_all()

        return MessageDelivery(queued.envelope, queued.delivery_count, ack=ack, nak=nak)

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()


type NatsConnector = Callable[[tuple[str, ...]], Awaitable[Any]]


async def _default_nats_connector(servers: tuple[str, ...]) -> Any:
    import nats

    return await nats.connect(
        servers=list(servers),
        name="enterprise-agent-platform",
        allow_reconnect=True,
        max_reconnect_attempts=-1,
    )


class NatsJetStreamBus:
    """Lazy NATS JetStream adapter with explicit durable-consumer ACK/NAK."""

    def __init__(
        self,
        servers: tuple[str, ...],
        stream: str,
        subjects: tuple[str, ...],
        replicas: int,
        connector: NatsConnector | None = None,
    ) -> None:
        if not servers or any(not server.startswith(("nats://", "tls://")) for server in servers):
            raise ValueError("NATS servers are invalid")
        if not isinstance(stream, str) or not _STREAM.fullmatch(stream):
            raise ValueError("stream must be a stable NATS stream name")
        if not subjects:
            raise ValueError("NATS stream requires subjects")
        if not 1 <= replicas <= 5:
            raise ValueError("NATS replicas must be between 1 and 5")
        self._servers = servers
        self._stream = stream
        self._subjects = subjects
        self._replicas = replicas
        self._connector = connector or _default_nats_connector
        self._connection: Any | None = None
        self._jetstream: Any | None = None
        self._subscriptions: dict[tuple[str, str], Any] = {}
        self._connect_lock = asyncio.Lock()

    async def _ensure_connected(self) -> Any:
        if self._jetstream is not None:
            return self._jetstream
        async with self._connect_lock:
            if self._jetstream is not None:
                return self._jetstream
            connection = await self._connector(self._servers)
            jetstream = connection.jetstream()
            try:
                await jetstream.stream_info(self._stream)
            except Exception as error:
                if error.__class__.__name__ not in {"NotFoundError", "BucketNotFoundError"}:
                    await connection.close()
                    raise
                await jetstream.add_stream(
                    name=self._stream,
                    subjects=list(self._subjects),
                    storage="file",
                    # nats-py StreamConfig field is num_replicas, not replicas
                    # (add_stream passes kwargs through dataclasses.replace).
                    num_replicas=self._replicas,
                )
            self._connection = connection
            self._jetstream = jetstream
            return jetstream

    async def publish(self, envelope: MessageEnvelope) -> None:
        jetstream = await self._ensure_connected()
        await jetstream.publish(
            envelope.topic,
            envelope.to_json_bytes(),
            headers={"Nats-Msg-Id": envelope.message_id},
        )

    async def pull(self, topic: str, *, consumer: str, timeout: float) -> MessageDelivery | None:
        if timeout <= 0:
            raise ValueError("pull timeout must be positive")
        _required_identifier("consumer", consumer)
        jetstream = await self._ensure_connected()
        key = (topic, consumer)
        subscription = self._subscriptions.get(key)
        if subscription is None:
            subscription = await jetstream.pull_subscribe(
                topic,
                durable=consumer,
                stream=self._stream,
            )
            self._subscriptions[key] = subscription
        try:
            messages = await subscription.fetch(1, timeout=timeout)
        except Exception as error:
            if error.__class__.__name__ in {"TimeoutError", "FetchTimeoutError"}:
                return None
            raise
        if not messages:
            return None
        raw = messages[0]
        envelope = MessageEnvelope.from_json_bytes(bytes(raw.data))
        metadata = getattr(raw, "metadata", None)
        delivery_count = int(getattr(metadata, "num_delivered", 1))

        async def ack() -> None:
            await raw.ack()

        async def nak() -> None:
            await raw.nak()

        return MessageDelivery(envelope, delivery_count, ack=ack, nak=nak)

    async def close(self) -> None:
        connection = self._connection
        self._connection = None
        self._jetstream = None
        self._subscriptions.clear()
        if connection is not None:
            await connection.drain()


type InboxHandler = Callable[[PlatformTransaction, MessageEnvelope], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class InboxConsumer:
    """Atomically applies one message and its processed Inbox marker."""

    store: PlatformStore
    handler_version: str

    def __post_init__(self) -> None:
        _required_identifier("handler_version", self.handler_version)

    async def process(self, envelope: MessageEnvelope, handler: InboxHandler) -> bool:
        async with self.store.transaction() as transaction:
            now = await transaction.db_now()
            record = InboxMessageRecord(
                tenant_id=envelope.tenant_id,
                message_id=envelope.message_id,
                handler_version=self.handler_version,
                topic=envelope.topic,
                payload_schema=envelope.payload_schema,
                payload_digest=envelope.payload_digest(),
                processing_state="RECEIVED",
                version=1,
                received_at=now,
                processed_at=None,
                failure_code=None,
            )
            if not await transaction.claim_inbox_message(record):
                existing = await transaction.get_inbox_message(
                    envelope.tenant_id,
                    envelope.message_id,
                    self.handler_version,
                )
                if existing.processing_state == "PROCESSED":
                    return False
                raise PlatformError(
                    "INBOX_MESSAGE_IN_PROGRESS",
                    "message was claimed without a completed business transaction",
                    retryable=True,
                )
            await handler(transaction, envelope)
            processed_at = await transaction.db_now()
            await transaction.replace_inbox_message_cas(
                replace(
                    record,
                    processing_state="PROCESSED",
                    version=record.version + 1,
                    processed_at=processed_at,
                ),
                record.version,
            )
            return True

    async def consume(self, delivery: MessageDelivery, handler: InboxHandler) -> bool:
        try:
            processed = await self.process(delivery.envelope, handler)
        except Exception:
            await delivery.nak()
            raise
        await delivery.ack()
        return processed


__all__ = [
    "InMemoryMessageBus",
    "InboxConsumer",
    "MessageBus",
    "MessageDelivery",
    "MessageEnvelope",
    "NatsJetStreamBus",
]
