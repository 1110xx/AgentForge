from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

from enterprise_agent_platform.contracts.events import EnterpriseEventEnvelope
from enterprise_agent_platform.contracts.models import (
    AttemptHistoryPage,
    AttemptSummary,
    ExecutionUnitSummary,
    RunEventPage,
    RunView,
    RunViewSnapshot,
    SurfaceRevision,
    SurfaceSummary,
)
from enterprise_agent_platform.domain.records import (
    AttemptRecord,
    ExecutionUnitRecord,
    RunRecord,
    UiSurfaceRecord,
)
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore

PUBLIC_REASON = re.compile(r"[A-Z][A-Z0-9_]{0,127}$")


@dataclass(frozen=True, slots=True)
class _ProjectionFacts:
    run: RunRecord
    units: tuple[ExecutionUnitRecord, ...]
    attempts: tuple[AttemptRecord, ...]
    surfaces: tuple[UiSurfaceRecord, ...]
    events: tuple[EnterpriseEventEnvelope, ...]
    retention_floor: int


def _sanitized_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    return reason if PUBLIC_REASON.fullmatch(reason) else "REDACTED"


class RunQueryService:
    def __init__(self, store: PlatformStore) -> None:
        self._store = store

    async def get_snapshot(self, tenant_id: str, run_id: str) -> RunViewSnapshot:
        facts = await self._read_facts(tenant_id, run_id)
        run = facts.run
        view = RunView(
            run_id=run.run_id,
            parent_run_id=run.parent_run_id,
            workflow_type=run.workflow_type,
            intent=run.intent,
            status=run.status,
            status_reason=_sanitized_reason(run.status_reason),
            version=run.version,
            created_at=run.created_at,
            updated_at=run.updated_at,
            ended_at=run.ended_at,
            execution_units=tuple(
                ExecutionUnitSummary(
                    execution_unit_id=unit.execution_unit_id,
                    role=unit.role,
                    status=unit.status,
                    version=unit.version,
                )
                for unit in sorted(facts.units, key=lambda item: item.execution_unit_id)
            ),
            attempts=tuple(
                AttemptSummary(
                    attempt_id=attempt.attempt_id,
                    execution_unit_id=attempt.execution_unit_id,
                    step_id=attempt.step_id,
                    status=attempt.status,
                    version=attempt.version,
                    started_at=attempt.started_at,
                    ended_at=attempt.ended_at,
                )
                for attempt in sorted(
                    facts.attempts,
                    key=lambda item: (item.created_at, item.attempt_id),
                )
            ),
            surfaces=tuple(
                SurfaceSummary(
                    surface_id=surface.surface_id,
                    catalog_id=surface.catalog_id,
                    revision=surface.current_revision,
                )
                for surface in sorted(facts.surfaces, key=lambda item: item.surface_id)
                if surface.status == "ACTIVE" and surface.current_revision is not None
            ),
            watermark=run.last_event_seq,
        )
        return RunViewSnapshot(
            schema_version="run-view-snapshot/v1",
            run_id=run.run_id,
            status=run.status,
            watermark=run.last_event_seq,
            view=view,
        )

    async def list_attempts(self, tenant_id: str, run_id: str) -> AttemptHistoryPage:
        """Full Attempt history for a Run (public API — no store direct access)."""
        async with self._store.transaction() as tx:
            # NOT_FOUND from get_run maps to the public 404 contract.
            await tx.get_run(tenant_id, run_id)
            attempts = await tx.list_attempts_for_run(tenant_id, run_id)
        ordered = tuple(sorted(attempts, key=lambda item: (item.created_at, item.attempt_id)))
        records = tuple(
            AttemptSummary(
                attempt_id=attempt.attempt_id,
                execution_unit_id=attempt.execution_unit_id,
                step_id=attempt.step_id,
                status=attempt.status,
                version=attempt.version,
                started_at=attempt.started_at,
                ended_at=attempt.ended_at,
            )
            for attempt in ordered
        )
        return AttemptHistoryPage(
            schema_version="attempt-history-page/v1",
            run_id=run_id,
            total_count=len(records),
            records=records,
        )

    async def get_events(
        self,
        tenant_id: str,
        run_id: str,
        *,
        after_event_seq: int,
        limit: int,
    ) -> RunEventPage:
        if type(after_event_seq) is not int or after_event_seq < 0:
            raise PlatformError("INVALID_EVENT_CURSOR", "event cursor must be non-negative")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise PlatformError("INVALID_EVENT_LIMIT", "event limit must be between 1 and 500")
        facts = await self._read_facts(tenant_id, run_id)
        watermark = facts.run.last_event_seq
        if after_event_seq < facts.retention_floor:
            raise PlatformError("RESYNC_REQUIRED", "event cursor precedes retention floor")
        if after_event_seq > watermark:
            raise PlatformError("EVENT_CURSOR_AHEAD", "event cursor exceeds Run watermark")
        selected = tuple(
            event
            for event in facts.events
            if after_event_seq < event.event_seq <= watermark
        )[:limit]
        return RunEventPage(
            schema_version="run-event-page/v1",
            run_id=run_id,
            after_event_seq=after_event_seq,
            watermark=watermark,
            retention_floor=facts.retention_floor,
            resync_required=False,
            events=selected,
        )

    async def get_surface_revision(
        self,
        tenant_id: str,
        run_id: str,
        surface_id: str,
        revision: int | None,
    ) -> SurfaceRevision:
        """Read one immutable A2UI surface revision for the browser SDK."""
        async with self._store.transaction() as tx:
            surface = await tx.get_ui_surface(tenant_id, surface_id)
            if surface is None or surface.run_id != run_id:
                raise PlatformError("NOT_FOUND", "surface was not found for this run")
            if surface.status != "ACTIVE" or surface.current_revision is None:
                raise PlatformError("NOT_FOUND", "surface has no published revision")
            selected = revision if revision is not None else surface.current_revision
            record = await tx.get_ui_surface_revision(tenant_id, surface_id, selected)
            if record.run_id != run_id:
                raise PlatformError("NOT_FOUND", "surface revision does not belong to this run")
        return SurfaceRevision(
            schema_version="a2ui-surface-revision/v0.9.1",
            surface_id=record.surface_id,
            run_id=record.run_id,
            revision=record.revision,
            source_attempt_id=record.source_attempt_id,
            source_event_seq=record.source_event_seq,
            document=record.document,
            checksum=record.checksum,
        )

    async def _read_facts(self, tenant_id: str, run_id: str) -> _ProjectionFacts:
        async with self._store.transaction() as tx:
            run = await tx.get_run(tenant_id, run_id)
            units = await tx.list_execution_units_for_run(tenant_id, run_id)
            attempts = await tx.list_attempts_for_run(tenant_id, run_id)
            surfaces = await tx.list_ui_surfaces_for_run(tenant_id, run_id)
            events = await tx.list_events_for_run(tenant_id, run_id)
            retention_floor = await tx.get_event_retention_floor(tenant_id, run_id)
        ordered_events = tuple(sorted(events, key=lambda item: item.event_seq))
        if any(event.tenant_id != tenant_id or event.run_id != run_id for event in ordered_events):
            raise PlatformError("INTEGRITY_VIOLATION", "projection contains foreign events")
        if any(
            current.event_seq <= previous.event_seq
            for previous, current in pairwise(ordered_events)
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "durable event sequence is not increasing")
        durable_watermark = ordered_events[-1].event_seq if ordered_events else 0
        if durable_watermark != run.last_event_seq:
            raise PlatformError(
                "INTEGRITY_VIOLATION", "Run watermark does not match durable events"
            )
        if retention_floor > run.last_event_seq:
            raise PlatformError(
                "INTEGRITY_VIOLATION", "event retention floor exceeds Run watermark"
            )
        retained_sequences = [
            event.event_seq for event in ordered_events if event.event_seq > retention_floor
        ]
        expected_sequences = list(range(retention_floor + 1, run.last_event_seq + 1))
        if retained_sequences != expected_sequences:
            raise PlatformError("INTEGRITY_VIOLATION", "durable events are not contiguous")
        return _ProjectionFacts(
            run=run,
            units=units,
            attempts=attempts,
            surfaces=surfaces,
            events=ordered_events,
            retention_floor=retention_floor,
        )
