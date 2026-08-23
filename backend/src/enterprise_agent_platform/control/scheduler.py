"""Fair admission of durable work into one Attempt/Lease reservation."""
from __future__ import annotations

import asyncio

from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.domain.records import DispatchTicket, SchedulableWork
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore
from enterprise_agent_platform.platform.telemetry import DiagnosticTelemetry


class FairScheduler:
    """Tenant round-robin scheduler for one elected scheduler process."""

    def __init__(
        self,
        store: PlatformStore,
        control_service: ControlPlaneService | None = None,
        telemetry: DiagnosticTelemetry | None = None,
    ) -> None:
        self._store = store
        self._control = control_service or ControlPlaneService(store, telemetry=telemetry)
        self._telemetry = telemetry
        self._last_tenant: str | None = None
        self._claim_lock = asyncio.Lock()

    async def claim_ready_work(self, worker_id: str) -> DispatchTicket | None:
        if not worker_id:
            raise PlatformError("INTEGRITY_VIOLATION", "worker_id is required")
        async with self._claim_lock:
            candidates = await self._store.list_schedulable_work()
            if self._telemetry is not None:
                try:
                    self._telemetry.record_gauge(
                        "agent_platform_queue_backlog",
                        float(len(candidates)),
                        labels={"operation": "scheduler.claim"},
                    )
                except Exception:  # noqa: BLE001, S110 - diagnostics never gate claims
                    pass
            tenants = sorted({candidate.run.tenant_id for candidate in candidates})
            if not tenants:
                return None
            ordered_tenants = self._rotate_after(tenants, self._last_tenant)
            by_tenant = {
                tenant_id: tuple(
                    candidate
                    for candidate in candidates
                    if candidate.run.tenant_id == tenant_id
                )
                for tenant_id in ordered_tenants
            }
            for tenant_id in ordered_tenants:
                for candidate in by_tenant[tenant_id]:
                    ticket = await self._try_claim(worker_id, candidate)
                    if ticket is not None:
                        self._last_tenant = tenant_id
                        return ticket
            return None

    @staticmethod
    def _rotate_after(tenants: list[str], previous: str | None) -> tuple[str, ...]:
        if previous not in tenants:
            return tuple(tenants)
        index = tenants.index(previous) + 1
        return tuple(tenants[index:] + tenants[:index])

    async def _try_claim(
        self, worker_id: str, candidate: SchedulableWork
    ) -> DispatchTicket | None:
        ctx = RequestContext(
            tenant_id=candidate.run.tenant_id,
            actor_id=f"scheduler:{worker_id}",
            scopes=("runs:execute",),
            request_id=(
                f"dispatch:{candidate.unit.execution_unit_id}:v{candidate.unit.version}:{worker_id}"
            ),
        )
        try:
            reservation = await self._control.reserve_attempt(
                ctx,
                candidate.unit.execution_unit_id,
                candidate.checkpoint.checkpoint_id,
                candidate.unit.version,
                transition_key=ctx.request_id,
            )
        except PlatformError as error:
            if error.code in (
                "ACTIVE_ATTEMPT_EXISTS",
                "ACTIVE_EXECUTION_EXISTS",
                "IDEMPOTENCY_KEY_REUSED",
                "INVALID_STATE",
                "SOURCE_CHECKPOINT_INVALID",
                "VERSION_CONFLICT",
            ):
                return None
            raise
        return DispatchTicket(
            worker_id=worker_id,
            tenant_id=candidate.run.tenant_id,
            run_id=candidate.run.run_id,
            execution_unit_id=candidate.unit.execution_unit_id,
            attempt_id=reservation.attempt.attempt_id,
            lease_id=reservation.lease.lease_id,
            generation=reservation.attempt.generation,
            source_checkpoint_id=candidate.checkpoint.checkpoint_id,
        )


async def claim_ready_work(scheduler: FairScheduler, worker_id: str) -> DispatchTicket | None:
    return await scheduler.claim_ready_work(worker_id)
