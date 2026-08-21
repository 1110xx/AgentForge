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

The terminal transitions (SUCCEEDED / FAILED) are delegated to the shared
``RunCompleter`` (execution/completer.py) — the same public completer used by
``SubprocessOrchestrator`` (SDD §13.2 risk 1: no more reuse of private methods).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.domain.records import DispatchTicket
from enterprise_agent_platform.execution.completer import RunCompleter
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
    completer: RunCompleter = field(init=False)

    def __post_init__(self) -> None:
        # Shared terminal-state machine (also used by SubprocessOrchestrator).
        self.completer = RunCompleter(self.store)

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
            except Exception as e:  # noqa: BLE001 - open failure is non-fatal; fall back to simulate
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
            except Exception as e:  # noqa: BLE001 - close failure is non-fatal
                logger.warning("Session close warning: %s", e)

        # ── 6. Complete or fail the run (shared RunCompleter) ──
        if session_completed:
            await self.completer.complete_run(ctx, ticket, run)
        else:
            await self.completer.fail_run(ctx, ticket, run, session_error)

        logger.info(
            "Runtime finished: run=%s attempt=%s completed=%s",
            ticket.run_id, ticket.attempt_id, session_completed,
        )


__all__ = ["LocalRuntime"]