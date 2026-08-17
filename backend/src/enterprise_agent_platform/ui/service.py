"""UI surface commit service.

Publishes immutable, content-addressed Surface revisions. Every revision is
backed by an enterprise event (ui.surface.committed), a checksummed revision
record and a CAS-advanced surface row; the A2UI document is validated against
the fixed catalog before anything is persisted.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from pydantic import JsonValue

from enterprise_agent_platform.contracts.enums import EventType
from enterprise_agent_platform.contracts.events import (
    EnterpriseEventEnvelope,
    UiSurfaceCommittedPayload,
)
from enterprise_agent_platform.contracts.models import StrictModel, SurfaceRevision
from enterprise_agent_platform.domain.records import (
    UiSurfaceRecord,
    UiSurfaceRevisionRecord,
)
from enterprise_agent_platform.persistence.protocol import PlatformError, PlatformStore
from enterprise_agent_platform.ui.catalog import A2UI_PROTOCOL_VERSION, PUBLIC_CATALOG_ID
from enterprise_agent_platform.ui.records import PublishedSurface, PublishedSurfaceRevision

_SURFACE_PAYLOAD_SCHEMA = "a2ui-surface/v0.9.1"


class SurfaceCommitRequest(StrictModel):
    tenant_id: str
    run_id: str
    surface_id: str
    source_attempt_id: str
    source_generation: int
    catalog_id: str
    protocol_version: str
    document: dict[str, JsonValue]
    trace_id: str | None = None


class ApprovalSurfaceRequest(StrictModel):
    tenant_id: str
    run_id: str
    surface_id: str
    approval_id: str
    title: str
    trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class SurfaceService:
    store: PlatformStore
    validator: object

    async def commit_revision(self, request: SurfaceCommitRequest) -> PublishedSurface:
        try:
            validation_result = self.validator.validate(request.document)
        except ValueError as error:
            raise PlatformError("SURFACE_INVALID", str(error)) from error
        payload = json.dumps(
            request.document, sort_keys=True, separators=(",", ":")
        ).encode()
        checksum = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        async with self.store.transaction() as tx:
            now = await tx.db_now()
            run = await tx.lock_run(request.tenant_id, request.run_id)
            surface = await tx.get_ui_surface(request.tenant_id, request.surface_id)
            revision = 1 if surface is None else surface.current_revision + 1
            event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=self.store.new_id("event"),
                tenant_id=request.tenant_id,
                run_id=request.run_id,
                event_seq=run.last_event_seq + 1,
                event_type=EventType.UI_SURFACE_COMMITTED,
                occurred_at=now,
                producer_service="control-plane",
                payload_schema=_SURFACE_PAYLOAD_SCHEMA,
                payload=UiSurfaceCommittedPayload(
                    kind="ui.surface.committed",
                    surface_id=request.surface_id,
                    revision=revision,
                ),
                attempt_id=request.source_attempt_id,
                causation_event_id=None,
                trace_id=request.trace_id,
            )
            if surface is None:
                await tx.insert_ui_surface(
                    UiSurfaceRecord(
                        tenant_id=request.tenant_id,
                        surface_id=request.surface_id,
                        run_id=request.run_id,
                        catalog_id=request.catalog_id,
                        protocol_version=request.protocol_version,
                        current_revision=None,
                        status="ACTIVE",
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                expected_surface_version = 1
            else:
                expected_surface_version = surface.version
            await tx.append_event(event, run.last_event_seq)
            await tx.insert_ui_surface_revision(
                UiSurfaceRevisionRecord(
                    tenant_id=request.tenant_id,
                    surface_id=request.surface_id,
                    revision=revision,
                    run_id=request.run_id,
                    source_attempt_id=request.source_attempt_id,
                    source_generation=request.source_generation,
                    source_event_seq=event.event_seq,
                    document=request.document,
                    checksum=checksum,
                    validation_result=validation_result,
                    created_at=now,
                )
            )
            if surface is None:
                await tx.replace_ui_surface_cas(
                    UiSurfaceRecord(
                        tenant_id=request.tenant_id,
                        surface_id=request.surface_id,
                        run_id=request.run_id,
                        catalog_id=request.catalog_id,
                        protocol_version=request.protocol_version,
                        current_revision=revision,
                        status="ACTIVE",
                        version=expected_surface_version + 1,
                        created_at=now,
                        updated_at=now,
                    ),
                    expected_surface_version,
                )
            else:
                await tx.replace_ui_surface_cas(
                    replace(
                        surface,
                        current_revision=revision,
                        version=surface.version + 1,
                        updated_at=now,
                    ),
                    surface.version,
                )
            await tx.replace_run_cas(
                replace(
                    run,
                    version=run.version + 1,
                    last_event_seq=event.event_seq,
                    updated_at=now,
                ),
                run.version,
            )
        return PublishedSurface(
            surface_id=request.surface_id,
            revision=PublishedSurfaceRevision(revision=revision),
            document=request.document,
        )

    async def commit_approval_surface(
        self, request: ApprovalSurfaceRequest
    ) -> PublishedSurface:
        approval = await self.store.get_approval_request(
            request.tenant_id, request.approval_id
        )
        proposal = await self.store.get_action_proposal(
            request.tenant_id, approval.action_ref
        )
        if proposal.run_id != request.run_id or approval.run_id != request.run_id:
            raise PlatformError(
                "INTEGRITY_VIOLATION",
                "approval surface is not bound to the run",
            )
        document: dict[str, JsonValue] = {
            "component": "ApprovalCard",
            "props": {
                "approval_id": request.approval_id,
                "approve_key": f"approval:{request.approval_id}:approve",
                "reject_key": f"approval:{request.approval_id}:reject",
                "title": request.title,
                "displayed_digest": approval.request_digest,
                "canonical_request_ref": approval.canonical_request_ref,
            },
        }
        return await self.commit_revision(
            SurfaceCommitRequest(
                tenant_id=request.tenant_id,
                run_id=request.run_id,
                surface_id=request.surface_id,
                source_attempt_id=proposal.attempt_id,
                source_generation=proposal.source_generation,
                catalog_id=PUBLIC_CATALOG_ID,
                protocol_version=A2UI_PROTOCOL_VERSION,
                document=document,
                trace_id=request.trace_id,
            )
        )

    async def revision_contract(
        self, tenant_id: str, surface_id: str, revision: int
    ) -> SurfaceRevision:
        record = await self.store.get_ui_surface_revision(
            tenant_id, surface_id, revision
        )
        return SurfaceRevision(
            schema_version="a2ui-surface-revision/v0.9.1",
            surface_id=surface_id,
            run_id=record.run_id,
            revision=revision,
            source_attempt_id=record.source_attempt_id,
            source_event_seq=record.source_event_seq,
            document=record.document,
            checksum=record.checksum,
        )
