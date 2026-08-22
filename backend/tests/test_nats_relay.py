"""NATS relay wiring unit tests (Phase 3, task 5).

Covers the resident loops that were missing: the outbox relay (drains PENDING
rows to the bus) and the wake-up consumer (records deliveries in the Inbox for
dedup and kicks the scheduler). Runs entirely against the in-process adapters
(InMemoryPlatformStore + InMemoryMessageBus) — the real NATS/PostgreSQL chain
is exercised by the L2 compose gate in tests/integration.
"""
from __future__ import annotations

import asyncio

import pytest

from enterprise_agent_platform.contracts.commands import CreateRunCommand
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.execution.scheduler_service import SchedulerService
from enterprise_agent_platform.persistence import InMemoryPlatformStore
from enterprise_agent_platform.platform.message_bus import (
    InMemoryMessageBus,
    MessageEnvelope,
)
from enterprise_agent_platform.platform.outbox import OutboxPublisher
from enterprise_agent_platform.platform.relay import (
    OutboxRelay,
    WakeupConsumer,
    create_relay_from_env,
)


def _envelope(
    message_id: str = "msg-1", topic: str = "agent.dispatch.requested"
) -> MessageEnvelope:
    return MessageEnvelope(
        message_id=message_id,
        tenant_id="demo-tenant",
        topic=topic,
        schema_version="platform-message/v1",
        payload_schema="dispatch.requested/v1",
        references={"run_id": "run-relay-1", "aggregate_version": 1},
    )


@pytest.mark.asyncio
async def test_relay_drains_outbox_to_bus() -> None:
    store = InMemoryPlatformStore()
    bus = InMemoryMessageBus()
    control = ControlPlaneService(store)
    relay = OutboxRelay(store=store, publisher=OutboxPublisher(store=store, bus=bus))

    # create_run appends a dispatch.requested outbox row in the same transaction.
    await control.create_run(
        RequestContext(
            tenant_id="demo-tenant",
            actor_id="relay-test",
            scopes=("runs:create", "runs:read"),
            request_id="relay-create-drain",
            trace_id="relay-trace-drain",
        ),
        CreateRunCommand(
            workflow_type="synthetic-analysis",
            intent="relay drain",
            resource_refs=("synthetic-case:case-42",),
            parameters={},
            host_context_ref="reference-context:test",
        ),
        "relay-idem-drain",
    )

    batch = await relay.run_once()
    assert batch.selected == 1
    assert batch.published == 1
    assert batch.deferred == 0
    assert batch.failed == 0

    # The row is CAS-transitioned to PUBLISHED and the bus holds the envelope.
    pending = await store.list_pending_outbox()
    assert pending == ()
    delivery = await bus.pull(
        "agent.dispatch.requested", consumer="test", timeout=1.0
    )
    assert delivery is not None
    assert delivery.envelope.topic == "agent.dispatch.requested"
    assert delivery.envelope.references["run_id"] != ""


@pytest.mark.asyncio
async def test_relay_marks_failed_publish_deferred() -> None:
    store = InMemoryPlatformStore()
    control = ControlPlaneService(store)

    class ExplodingBus(InMemoryMessageBus):
        async def publish(self, envelope: MessageEnvelope) -> None:  # type: ignore[override]
            raise RuntimeError("broker down")

    relay = OutboxRelay(
        store=store,
        publisher=OutboxPublisher(store=store, bus=ExplodingBus(), max_attempts=3),
    )
    await control.create_run(
        RequestContext(
            tenant_id="demo-tenant",
            actor_id="relay-test",
            scopes=("runs:create", "runs:read"),
            request_id="relay-create-deferred",
            trace_id="relay-trace-deferred",
        ),
        CreateRunCommand(
            workflow_type="synthetic-analysis",
            intent="relay deferred",
            resource_refs=("synthetic-case:case-42",),
            parameters={},
            host_context_ref="reference-context:test",
        ),
        "relay-idem-deferred",
    )

    batch = await relay.run_once()
    assert batch.selected == 1
    assert batch.published == 0
    assert batch.deferred == 1  # not yet terminal; retry later with backoff

    rows = await store.list_outbox("demo-tenant")
    assert rows[0].publish_state == "PENDING"
    assert rows[0].delivery_attempts == 1
    assert rows[0].last_error_code == "MESSAGE_BUS_PUBLISH_FAILED"
    assert rows[0].next_attempt_at is not None


@pytest.mark.asyncio
async def test_wakeup_consumer_applies_inbox_and_kicks() -> None:
    store = InMemoryPlatformStore()
    bus = InMemoryMessageBus()
    kicks: list[str] = []

    consumer = WakeupConsumer(
        store=store,
        bus=bus,
        subjects=("agent.dispatch.requested",),
        wake=lambda: kicks.append("kick"),
    )
    await bus.publish(_envelope())

    consume_task = asyncio.create_task(consumer.consume_loop())
    try:
        for _ in range(50):
            if kicks:
                break
            await asyncio.sleep(0.02)
        consume_task.cancel()
        await consume_task
    finally:
        if not consume_task.done():
            consume_task.cancel()

    assert kicks, "the wake callback must be invoked for a dispatch.requested delivery"
    inbox = await store.get_inbox_message(
        "demo-tenant", "msg-1", consumer.handler_version
    )
    assert inbox.processing_state == "PROCESSED"
    assert inbox.topic == "agent.dispatch.requested"


@pytest.mark.asyncio
async def test_wakeup_consumer_deduplicates_messages() -> None:
    store = InMemoryPlatformStore()
    bus = InMemoryMessageBus()
    kicks: list[str] = []

    consumer = WakeupConsumer(
        store=store,
        bus=bus,
        subjects=("agent.dispatch.requested",),
        wake=lambda: kicks.append("kick"),
    )
    await bus.publish(_envelope(message_id="msg-dupe"))
    await bus.publish(_envelope(message_id="msg-dupe"))

    consume_task = asyncio.create_task(consumer.consume_loop())
    try:
        # Let the consumer process both deliveries (short back-to-back pulls).
        for _ in range(100):
            if len(kicks) >= 2:
                break
            await asyncio.sleep(0.02)
        consume_task.cancel()
        await consume_task
    finally:
        if not consume_task.done():
            consume_task.cancel()

    # First delivery applies; the duplicate is rejected by the Inbox claim.
    assert len(kicks) == 1, "duplicate message_id must be applied exactly once"
    inbox = await store.get_inbox_message(
        "demo-tenant", "msg-dupe", consumer.handler_version
    )
    assert inbox.processing_state == "PROCESSED"


@pytest.mark.asyncio
async def test_scheduler_wake_triggers_immediate_claim() -> None:
    store = InMemoryPlatformStore()
    control = ControlPlaneService(store)
    from enterprise_agent_platform.contracts.commands import CreateRunCommand

    ctx = RequestContext(
        tenant_id="demo-tenant",
        actor_id="relay-test",
        scopes=("runs:create", "runs:read", "runs:execute"),
        request_id="relay-create-1",
        trace_id="relay-trace-1",
    )
    run = await control.create_run(
        ctx,
        CreateRunCommand(
            workflow_type="synthetic-analysis",
            intent="relay wake claim",
            resource_refs=("synthetic-case:case-42",),
            parameters={},
            host_context_ref="reference-context:test",
        ),
        "relay-idem-2",
    )
    class InstantOrchestrator:
        """Resolves the reserved Attempt immediately (no long runtime)."""

        async def execute(self, ticket: object) -> None:
            return None

    scheduler = SchedulerService(
        store=store,
        control=control,
        orchestrator=InstantOrchestrator(),
        poll_interval=30.0,
    )

    loop_task = asyncio.create_task(scheduler.run_loop())
    try:
        # Long poll interval: without a wake the scheduler would not claim soon.
        assert not scheduler._wake_event.is_set()
        scheduler.wake()
        assert scheduler._wake_event.is_set()

        # The wake must lead to an attempt reservation within a couple of ticks.
        attempts = ()
        for _ in range(100):
            async with store.transaction() as tx:
                attempts = await tx.list_attempts_for_run(
                    ctx.tenant_id, run.run_id
                )
            if attempts:
                break
            await asyncio.sleep(0.02)
        assert attempts, "wake signal must result in an Attempt reservation"
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


def test_create_relay_from_env_gates_on_nats_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_PLATFORM_NATS_URL", raising=False)
    assert create_relay_from_env(InMemoryPlatformStore()) is None

    monkeypatch.setenv("AGENT_PLATFORM_NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.setenv("AGENT_PLATFORM_NATS_STREAM", "AGENT_PLATFORM")
    services = create_relay_from_env(InMemoryPlatformStore())
    assert services is not None
    assert services.consumer.subjects == (
        "agent.dispatch.requested",
        "agent.attempt.provisioning.requested",
        "agent.scheduler.work.ready",
    )
    monkeypatch.delenv("AGENT_PLATFORM_NATS_URL", raising=False)


def test_relay_subject_prefix_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PLATFORM_NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.setenv("AGENT_PLATFORM_NATS_SUBJECT_PREFIX", "platform")
    services = create_relay_from_env(InMemoryPlatformStore())
    assert services is not None
    assert services.consumer.subjects == (
        "platform.dispatch.requested",
        "platform.attempt.provisioning.requested",
        "platform.scheduler.work.ready",
    )
    monkeypatch.delenv("AGENT_PLATFORM_NATS_URL", raising=False)