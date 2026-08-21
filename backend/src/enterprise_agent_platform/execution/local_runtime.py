"""In-process Runtime — executes a claimed Attempt locally without a K8s Pod.

In production, the Runtime runs inside a Sandbox Pod. In the local demo, it
runs in the same process, calling the model provider directly and emitting
events through the store.

Execution flow:
  1. Activate lease → Run transitions QUEUED → RUNNING, emits events
  2. Open model session via RunSessionProvider
  3. Run the agent loop (run_task) — calls model, generates surfaces, etc.
  4. Periodically renew lease (heartbeat)
  5. On completion → commit final checkpoint, transition Run to SUCCEEDED/FAILED
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast

from pydantic import JsonValue

from enterprise_agent_platform.contracts.enums import (
    AttemptState,
    CheckpointState,
    EntityType,
    EventType,
    ExecutionLeaseState,
    ExecutionUnitState,
    RunState,
)
from enterprise_agent_platform.contracts.events import (
    AttemptLifecyclePayload,
    EnterpriseEventEnvelope,
    RunCreatedPayload,
    RunStatusChangedPayload,
    UiSurfaceCommittedPayload,
)
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.domain.fsm import CompleteCancellation, transition
from enterprise_agent_platform.domain.records import (
    AttemptRecord,
    AuditEventRecord,
    CheckpointRecord,
    DispatchTicket,
    ExecutionLeaseRecord,
    ExecutionUnitRecord,
    OutboxMessageRecord,
    RunRecord,
)
from enterprise_agent_platform.execution.session import RunSessionProvider
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore

logger = logging.getLogger(__name__)


def _digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(slots=True)
class LocalRuntime:
    """In-process runtime that executes a claimed Attempt locally.

    This replaces the K8s Sandbox Pod Runtime for local development. It drives
    the Run through its lifecycle: activate → run → complete/fail, emitting
    all events along the way.
    """

    store: PlatformStore
    control: ControlPlaneService
    run_sessions: RunSessionProvider | None = None
    heartbeat_interval: float = 30.0
    lease_ttl: timedelta = timedelta(minutes=2)

    async def execute(self, ticket: DispatchTicket) -> None:
        """Execute one run end-to-end from a claimed DispatchTicket."""
        logger.info(
            "Runtime executing: run=%s attempt=%s gen=%d",
            ticket.run_id, ticket.attempt_id, ticket.generation,
        )

        # ── 1. Create a runtime identity context ──
        ctx = RequestContext(
            tenant_id=ticket.tenant_id,
            actor_id=f"runtime:local:{ticket.worker_id}",
            scopes=(
                "runs:execute",
                "runs:read",
                "runs:write",
                "actions:execute",
                "effects:recover",
                "approvals:decide",
            ),
            request_id=f"runtime-exec:{ticket.run_id}:{ticket.attempt_id}",
            trace_id=f"trace:{ticket.run_id}",
        )

        # ── 2. Activate lease → Run transitions QUEUED → RUNNING ──
        try:
            lease = await self.control.activate_lease(
                ctx,
                ticket.attempt_id,
                ticket.generation,
                owner=f"local-runtime:{ticket.worker_id}",
                expected_lease_version=1,  # fresh lease starts at version 1
            )
        except PlatformError as e:
            logger.error("activate_lease failed for run=%s: %s", ticket.run_id, e)
            return

        logger.info("Lease activated: run=%s attempt=%s", ticket.run_id, ticket.attempt_id)

        # ── 3. Open a model session ──
        run = await self.store.get_run(ticket.tenant_id, ticket.run_id)
        handle = None
        if self.run_sessions is not None:
            try:
                handle = await self.run_sessions.open(
                    run_id=run.run_id,
                    intent=run.intent,
                    resource_refs=run.resource_refs,
                    host_context_ref=run.host_context_ref,
                )
                logger.info("Session opened: run=%s session=%s", run.run_id, handle.session_id)
            except Exception as e:
                logger.warning("Session open failed (non-fatal): %s", e)

        # ── 4. Run the agent loop (heartbeat + run_task) ──
        #     Runs run_task() in background while heartbeating
        #     Heartbeat checks are done using a short polling loop so that
        #     a quick run_task() doesn't block for the full heartbeat interval.
        session_completed = False
        session_error: Exception | None = None
        try:
            if handle is not None:
                # Launch run_task in a concurrent task
                async def _run_task_wrapper() -> None:
                    nonlocal session_completed, session_error
                    try:
                        await self.run_sessions.run_task(handle)
                        session_completed = True
                    except Exception as e:
                        session_error = e
                        logger.exception("run_task failed for run=%s", run.run_id)

                task = asyncio.create_task(_run_task_wrapper())

                # Heartbeat loop: poll task completion every 2s, renew every heartbeat_interval
                try:
                    lease_version = lease.version
                    next_heartbeat = asyncio.get_event_loop().time() + self.heartbeat_interval
                    while not task.done():
                        remaining = next_heartbeat - asyncio.get_event_loop().time()
                        if remaining <= 0:
                            # Time to renew lease
                            try:
                                lease = await self.control.renew_lease(
                                    ctx,
                                    ticket.attempt_id,
                                    ticket.generation,
                                    owner=f"local-runtime:{ticket.worker_id}",
                                    expected_lease_version=lease_version,
                                )
                                lease_version = lease.version
                                logger.debug(
                                    "Lease renewed: run=%s attempt=%s version=%d",
                                    ticket.run_id, ticket.attempt_id, lease_version,
                                )
                            except PlatformError as e:
                                logger.error("Lease renewal failed: %s", e)
                                break
                            next_heartbeat = asyncio.get_event_loop().time() + self.heartbeat_interval
                        else:
                            # Wait for task completion with a short timeout
                            done, _ = await asyncio.wait(
                                [task], timeout=min(remaining, 2.0)
                            )
                            if done:
                                break
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.wait([task])

            else:
                # No session provider — simulate a brief "execution"
                logger.info("No session provider, simulating execution for run=%s", run.run_id)
                await asyncio.sleep(1)
                session_completed = True

        except Exception as e:
            session_error = e
            logger.exception("Runtime loop failed for run=%s", run.run_id)

        # ── 5. Close session ──
        if handle is not None:
            try:
                await self.run_sessions.close(handle)
            except Exception as e:
                logger.warning("Session close warning: %s", e)

        # ── 6. Complete or fail the run ──
        if session_completed:
            await self._complete_run(ctx, ticket, run)
        else:
            await self._fail_run(ctx, ticket, run, session_error)

        logger.info(
            "Runtime finished: run=%s attempt=%s completed=%s",
            ticket.run_id, ticket.attempt_id, session_completed,
        )

    async def _complete_run(
        self,
        ctx: RequestContext,
        ticket: DispatchTicket,
        run: RunRecord,
    ) -> None:
        """Transition the Run to SUCCEEDED with proper events."""
        async with self.store.transaction() as tx:
            now = await tx.db_now()

            # Lock all aggregates
            run = await tx.lock_run(ticket.tenant_id, run.run_id)
            unit = await tx.lock_execution_unit(ticket.tenant_id, ticket.execution_unit_id)
            attempt = await tx.get_attempt(ticket.tenant_id, ticket.attempt_id)
            lease = await tx.get_lease_for_attempt(ticket.tenant_id, ticket.attempt_id)

            # Validate state
            if run.status not in {RunState.QUEUED, RunState.RUNNING}:
                logger.warning("Run %s not in RUNNING state, skipping terminal transition", run.run_id)
                return

            # Transition: Attempt PROVISIONING → CLAIMED → RUNNING → SUCCEEDED
            transition(EntityType.ATTEMPT, attempt.status, AttemptState.RUNNING, None)
            running_attempt = replace(
                attempt,
                status=AttemptState.RUNNING,
                version=attempt.version + 1,
                updated_at=now,
            )
            await tx.replace_attempt_cas(running_attempt, attempt.version)

            # Attempt SUCCEEDED
            transition(EntityType.ATTEMPT, running_attempt.status, AttemptState.SUCCEEDED, None)
            succeeded_attempt = replace(
                running_attempt,
                status=AttemptState.SUCCEEDED,
                version=running_attempt.version + 1,
                updated_at=now,
                ended_at=now,
            )

            # Lease RELEASED
            transition(EntityType.EXECUTION_LEASE, lease.state, ExecutionLeaseState.RELEASED, None)
            released_lease = replace(
                lease,
                state=ExecutionLeaseState.RELEASED,
                version=lease.version + 1,
                released_at=now,
                updated_at=now,
            )

            # Unit SUCCEEDED
            transition(EntityType.EXECUTION_UNIT, unit.status, ExecutionUnitState.SUCCEEDED, None)
            succeeded_unit = replace(
                unit,
                status=ExecutionUnitState.SUCCEEDED,
                version=unit.version + 1,
                updated_at=now,
            )

            # Run SUCCEEDED
            transition(EntityType.RUN, run.status, RunState.SUCCEEDED, None)
            succeeded_run = replace(
                run,
                status=RunState.SUCCEEDED,
                status_reason=None,
                version=run.version + 1,
                last_event_seq=run.last_event_seq + 2,
                updated_at=now,
                ended_at=now,
            )

            # Events
            attempt_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self.store.new_id("event"),
                tenant_id=ticket.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 1,
                event_type=EventType.ATTEMPT_LIFECYCLE,
                occurred_at=now,
                producer_service="local-runtime",
                payload_schema="attempt-lifecycle/v1",
                payload=AttemptLifecyclePayload(
                    kind="attempt.lifecycle",
                    attempt_id=ticket.attempt_id,
                    status=AttemptState.SUCCEEDED,
                ),
                attempt_id=ticket.attempt_id,
                trace_id=ctx.trace_id,
            )
            run_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self.store.new_id("event"),
                tenant_id=ticket.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 2,
                event_type=EventType.RUN_STATUS_CHANGED,
                occurred_at=now,
                producer_service="local-runtime",
                payload_schema="run-status/v1",
                payload=RunStatusChangedPayload(
                    kind="run.status.changed",
                    previous=run.status,
                    current=RunState.SUCCEEDED,
                ),
                attempt_id=ticket.attempt_id,
                causation_event_id=attempt_event.event_id,
                trace_id=ctx.trace_id,
            )

            # Persist
            await tx.replace_attempt_cas(succeeded_attempt, running_attempt.version)
            await tx.replace_lease_cas(released_lease, lease.version)
            await tx.replace_execution_unit_cas(succeeded_unit, unit.version)
            await tx.replace_run_cas(succeeded_run, run.version)
            await tx.append_event(attempt_event, run.last_event_seq)
            await tx.append_event(run_event, attempt_event.event_seq)

            # Audit + outbox
            await tx.insert_audit(
                AuditEventRecord(
                    tenant_id=ticket.tenant_id,
                    audit_event_id=self.store.new_id("audit"),
                    run_id=run.run_id,
                    actor_id=ctx.actor_id,
                    action="run.succeeded.local-runtime",
                    entity_type="run",
                    entity_id=run.run_id,
                    entity_version=succeeded_run.version,
                    outcome="SUCCEEDED",
                    trace_id=ctx.trace_id,
                    details={"attempt_id": ticket.attempt_id},
                    created_at=now,
                )
            )
            await tx.insert_outbox(
                OutboxMessageRecord(
                    tenant_id=ticket.tenant_id,
                    message_id=self.store.new_id("outbox"),
                    run_id=run.run_id,
                    topic="run.terminal",
                    payload={"run_id": run.run_id},
                    event_id=run_event.event_id,
                    aggregate_version=succeeded_run.version,
                    created_at=now,
                    published_at=None,
                )
            )

    async def _fail_run(
        self,
        ctx: RequestContext,
        ticket: DispatchTicket,
        run: RunRecord,
        error: Exception | None,
    ) -> None:
        """Transition the Run to FAILED with proper events."""
        async with self.store.transaction() as tx:
            now = await tx.db_now()

            run = await tx.lock_run(ticket.tenant_id, run.run_id)
            unit = await tx.lock_execution_unit(ticket.tenant_id, ticket.execution_unit_id)
            attempt = await tx.get_attempt(ticket.tenant_id, ticket.attempt_id)
            lease = await tx.get_lease_for_attempt(ticket.tenant_id, ticket.attempt_id)

            if run.status in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
                logger.warning("Run %s already terminal, skipping fail", run.run_id)
                return

            # Attempt FAILED
            transition(EntityType.ATTEMPT, attempt.status, AttemptState.FAILED, None)
            failed_attempt = replace(
                attempt,
                status=AttemptState.FAILED,
                version=attempt.version + 1,
                updated_at=now,
                ended_at=now,
            )

            # Lease RELEASED
            try:
                transition(EntityType.EXECUTION_LEASE, lease.state, ExecutionLeaseState.RELEASED, None)
            except Exception:
                lease_state = ExecutionLeaseState.EXPIRED
            else:
                lease_state = ExecutionLeaseState.RELEASED
            released_lease = replace(
                lease,
                state=lease_state,
                version=lease.version + 1,
                released_at=now,
                updated_at=now,
            )

            # Unit FAILED
            try:
                transition(EntityType.EXECUTION_UNIT, unit.status, ExecutionUnitState.FAILED, None)
                failed_unit = replace(
                    unit,
                    status=ExecutionUnitState.FAILED,
                    version=unit.version + 1,
                    updated_at=now,
                )
            except Exception:
                failed_unit = replace(
                    unit,
                    version=unit.version + 1,
                    updated_at=now,
                )

            # Run FAILED
            try:
                transition(EntityType.RUN, run.status, RunState.FAILED, None)
            except Exception:
                pass  # force terminal
            failed_run = replace(
                run,
                status=RunState.FAILED,
                status_reason=str(error) if error else "Runtime execution failed",
                version=run.version + 1,
                last_event_seq=run.last_event_seq + 2,
                updated_at=now,
                ended_at=now,
            )

            # Events
            attempt_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self.store.new_id("event"),
                tenant_id=ticket.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 1,
                event_type=EventType.ATTEMPT_LIFECYCLE,
                occurred_at=now,
                producer_service="local-runtime",
                payload_schema="attempt-lifecycle/v1",
                payload=AttemptLifecyclePayload(
                    kind="attempt.lifecycle",
                    attempt_id=ticket.attempt_id,
                    status=AttemptState.FAILED,
                ),
                attempt_id=ticket.attempt_id,
                trace_id=ctx.trace_id,
            )
            run_event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self.store.new_id("event"),
                tenant_id=ticket.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 2,
                event_type=EventType.RUN_STATUS_CHANGED,
                occurred_at=now,
                producer_service="local-runtime",
                payload_schema="run-status/v1",
                payload=RunStatusChangedPayload(
                    kind="run.status.changed",
                    previous=run.status,
                    current=RunState.FAILED,
                ),
                attempt_id=ticket.attempt_id,
                causation_event_id=attempt_event.event_id,
                trace_id=ctx.trace_id,
            )

            await tx.replace_attempt_cas(failed_attempt, attempt.version)
            await tx.replace_lease_cas(released_lease, lease.version)
            await tx.replace_execution_unit_cas(failed_unit, unit.version)
            await tx.replace_run_cas(failed_run, run.version)
            await tx.append_event(attempt_event, run.last_event_seq)
            await tx.append_event(run_event, attempt_event.event_seq)

            await tx.insert_audit(
                AuditEventRecord(
                    tenant_id=ticket.tenant_id,
                    audit_event_id=self.store.new_id("audit"),
                    run_id=run.run_id,
                    actor_id=ctx.actor_id,
                    action="run.failed.local-runtime",
                    entity_type="run",
                    entity_id=run.run_id,
                    entity_version=failed_run.version,
                    outcome="FAILED",
                    trace_id=ctx.trace_id,
                    details={
                        "attempt_id": ticket.attempt_id,
                        "error": str(error) if error else None,
                    },
                    created_at=now,
                )
            )
            await tx.insert_outbox(
                OutboxMessageRecord(
                    tenant_id=ticket.tenant_id,
                    message_id=self.store.new_id("outbox"),
                    run_id=run.run_id,
                    topic="run.terminal",
                    payload={"run_id": run.run_id},
                    event_id=run_event.event_id,
                    aggregate_version=failed_run.version,
                    created_at=now,
                    published_at=None,
                )
            )