"""At-least-once PostgreSQL Outbox publisher for the wake-up message bus."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

from enterprise_agent_platform.domain.records import OutboxMessageRecord
from enterprise_agent_platform.persistence.protocol import PlatformStore

from .message_bus import MessageBus, MessageEnvelope


@dataclass(frozen=True, slots=True)
class OutboxPublishBatch:
    selected: int
    published: int
    deferred: int
    failed: int


def _transport_envelope(record: OutboxMessageRecord, *, subject_prefix: str) -> MessageEnvelope:
    references = {key: value for key, value in record.payload.items() if value is not None}
    if "revision" in references:
        references["surface_revision"] = references.pop("revision")
    references.setdefault("run_id", record.run_id)
    references.setdefault("aggregate_version", record.aggregate_version)
    if record.event_id is not None:
        references.setdefault("event_id", record.event_id)
    return MessageEnvelope(
        message_id=record.message_id,
        tenant_id=record.tenant_id,
        topic=f"{subject_prefix}.{record.topic}",
        schema_version="platform-message/v1",
        payload_schema=f"{record.topic}/v1",
        references=references,
        causation_event_id=record.event_id,
    )


class OutboxPublisher:
    """Publish stable references, then CAS the durable row to PUBLISHED.

    A process crash after broker ACK and before the CAS can publish a duplicate. The
    consumer must commit an Inbox marker before ACK. No implementation claims impossible
    exactly-once delivery across PostgreSQL and NATS.
    """

    def __init__(
        self,
        *,
        store: PlatformStore,
        bus: MessageBus,
        subject_prefix: str = "agent",
        max_attempts: int = 20,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 300.0,
    ) -> None:
        if not subject_prefix or "." in subject_prefix:
            raise ValueError("subject prefix must be one NATS token")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if retry_base_seconds <= 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("outbox retry delays are invalid")
        self._store = store
        self._bus = bus
        self._subject_prefix = subject_prefix
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    async def run_once(self, *, limit: int = 100) -> OutboxPublishBatch:
        records = await self._store.list_pending_outbox(limit=limit)
        published = deferred = failed = 0
        for record in records:
            try:
                envelope = _transport_envelope(record, subject_prefix=self._subject_prefix)
                await self._bus.publish(envelope)
            except Exception as error:  # noqa: BLE001 - adapter failures are retry facts
                terminal = await self._record_failure(record, error)
                failed += int(terminal)
                deferred += int(not terminal)
            else:
                published += int(await self._record_published(record))
        return OutboxPublishBatch(
            selected=len(records),
            published=published,
            deferred=deferred,
            failed=failed,
        )

    async def _record_published(self, selected: OutboxMessageRecord) -> bool:
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            current = await tx.get_outbox_message(selected.tenant_id, selected.message_id)
            if current.publish_state == "PUBLISHED":
                return False
            published = replace(
                current,
                publish_state="PUBLISHED",
                version=current.version + 1,
                delivery_attempts=current.delivery_attempts + 1,
                next_attempt_at=None,
                last_error=None,
                last_error_code=None,
                published_at=now,
            )
            await tx.replace_outbox_cas(published, current.version)
            return True

    async def _record_failure(self, selected: OutboxMessageRecord, error: Exception) -> bool:
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            current = await tx.get_outbox_message(selected.tenant_id, selected.message_id)
            if current.publish_state == "PUBLISHED":
                return False
            attempts = current.delivery_attempts + 1
            terminal = attempts >= self._max_attempts
            delay = min(
                self._retry_max_seconds,
                self._retry_base_seconds * (2 ** (attempts - 1)),
            )
            deferred = replace(
                current,
                publish_state="FAILED" if terminal else "PENDING",
                version=current.version + 1,
                delivery_attempts=attempts,
                next_attempt_at=None if terminal else now + timedelta(seconds=delay),
                last_error=type(error).__name__,
                last_error_code="MESSAGE_BUS_PUBLISH_FAILED",
                published_at=None,
            )
            await tx.replace_outbox_cas(deferred, current.version)
            return terminal


__all__ = ["OutboxPublishBatch", "OutboxPublisher"]
