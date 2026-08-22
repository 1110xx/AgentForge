"""Background scheduler loop — polls for QUEUED work and dispatches to runtime.

This is the missing 「Scheduler」 component in the execution chain:

  Scheduler ──→ Orchestrator ──→ Runtime (Sandbox Pod)
      │               │                 │
      │  轮询可调度    │  创建 K8s Job   │  执行真实工作流
      │  工作，领取    │  绑定 Attempt   │  调用模型、生成
      │  Attempt+Lease │  与 Pod         │  Surface、审批、
      │               │                 │  Effect、事件

The dispatcher is injectable:

* ``LocalRuntime`` — in-process execution (original demo, no isolation)
* ``SubprocessOrchestrator`` — one child process per Attempt over a JSON-line
  pipe (Phase-1 local development; mirrors per-task Pod isolation)
* ``KubernetesOrchestrator`` — production: one K8s Job/Pod per Attempt

The Scheduler itself always does the same job: polling, claiming, and
dispatching to the injected runtime/orchestrator.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from enterprise_agent_platform.control.scheduler import FairScheduler
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.domain.records import DispatchTicket
from enterprise_agent_platform.execution.local_runtime import LocalRuntime
from enterprise_agent_platform.execution.session import RunSessionProvider
from enterprise_agent_platform.persistence.protocol import PlatformStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SchedulerService:
    """Background scheduler that continuously polls and dispatches work.

    Usage in run.py::

        scheduler = SchedulerService(store, control, run_sessions)
        scheduler_task = asyncio.create_task(scheduler.run_loop())

        # ... start FastAPI server ...

        scheduler.cancel()  # on shutdown
    """

    store: PlatformStore
    control: ControlPlaneService
    run_sessions: RunSessionProvider | None = None
    orchestrator: object | None = None
    poll_interval: float = 2.0
    worker_id: str = field(default_factory=lambda: f"scheduler:{id(object()):x}")
    _scheduler: FairScheduler = field(init=False, repr=False)
    _runtime: object = field(init=False, repr=False)
    _task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _active_tickets: set[str] = field(default_factory=set, init=False, repr=False)
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, '_scheduler', FairScheduler(self.store, self.control))
        runtime: object
        if self.orchestrator is not None:
            runtime = self.orchestrator
        else:
            runtime = LocalRuntime(
                store=self.store,
                control=self.control,
                run_sessions=self.run_sessions,
            )
        object.__setattr__(self, '_runtime', runtime)

    async def run_loop(self) -> None:
        """Run the scheduling loop until cancelled."""
        object.__setattr__(self, "_task", asyncio.current_task())
        logger.info(
            "SchedulerService started (worker=%s, poll_interval=%.1fs)",
            self.worker_id,
            self.poll_interval,
        )
        try:
            while True:
                await self._tick()
                # Sleep for the poll interval unless a wake-up delivery (NATS
                # inbox) signals that new work may exist — claim immediately
                # instead of waiting out the full poll cycle.
                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(), timeout=self.poll_interval
                    )
                except TimeoutError:
                    pass
                finally:
                    self._wake_event.clear()
        except asyncio.CancelledError:
            logger.info("SchedulerService stopped")

    def wake(self) -> None:
        """Signal that new work may be schedulable (e.g. a NATS wake-up message).

        Safe to call from another coroutine or thread: ``set`` only marks the
        event; admission still happens through the poll + CAS claim so a stray
        wake is harmless.
        """
        if self._task is None or not self._task.done():
            self._wake_event.set()

    async def _tick(self) -> None:
        """One scheduling tick: claim at most one piece of work."""
        try:
            ticket = await self._scheduler.claim_ready_work(self.worker_id)
        except Exception:
            logger.exception("Scheduler claim_ready_work failed")
            return

        if ticket is None:
            return

        logger.info(
            "Scheduler claimed work: run=%s attempt=%s generation=%d",
            ticket.run_id,
            ticket.attempt_id,
            ticket.generation,
        )

        # Track the ticket to avoid re-dispatch
        dedup_key = f"{ticket.run_id}:{ticket.attempt_id}"
        if dedup_key in self._active_tickets:
            logger.warning("Skipping duplicate ticket %s", dedup_key)
            return
        self._active_tickets.add(dedup_key)

        # Dispatch to runtime in a separate task
        asyncio.create_task(self._execute_run(ticket, dedup_key))

    async def _execute_run(self, ticket: DispatchTicket, dedup_key: str) -> None:
        """Execute one claimed Run in-process."""
        try:
            await self._runtime.execute(ticket)
        except Exception:
            logger.exception("Runtime execution failed for run=%s", ticket.run_id)
        finally:
            self._active_tickets.discard(dedup_key)

    def cancel(self) -> None:
        """Cancel the scheduler loop task."""
        if self._task is not None and not self._task.done():
            self._task.cancel()