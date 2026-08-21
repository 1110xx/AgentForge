"""Transactional orchestration commands over stable domain records."""
from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import cast

from pydantic import JsonValue

from enterprise_agent_platform.contracts.commands import CreateRunCommand
from enterprise_agent_platform.contracts.enums import (
    AttemptState,
    CheckpointState,
    EntityType,
    EventType,
    ExecutionLeaseState,
    ExecutionUnitState,
    RunState,
)
from enterprise_agent_platform.domain.fsm import transition as _fsm
from enterprise_agent_platform.contracts.events import (
    AttemptLifecyclePayload,
    EnterpriseEventEnvelope,
    RunCreatedPayload,
    RunStatusChangedPayload,
)
from enterprise_agent_platform.domain.records import (
    AttemptRecord,
    AttemptReservation,
    AuditEventRecord,
    CheckpointRecord,
    ExecutionLeaseRecord,
    ExecutionUnitRecord,
    FollowupRequestRecord,
    IdempotencyRecord,
    OutboxMessageRecord,
    RunAuthorizationContext,
    RunAuthorizationSnapshotRecord,
    RunRecord,
)
from enterprise_agent_platform.persistence.protocol import (
    PlatformError,
    PlatformStore,
    PlatformTransaction,
)

from .context import RequestContext


def _request_digest(ctx: RequestContext, operation: str, payload: object) -> str:
    if hasattr(payload, "model_dump"):
        value = payload.model_dump(mode="json")
    else:
        value = _json_value(payload)
    canonical = json.dumps(
        {
            "actor_id": ctx.actor_id,
            "operation": operation,
            "payload": value,
            "tenant_id": ctx.tenant_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _json_value(value: object) -> JsonValue:
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return cast(str, value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise PlatformError("INTEGRITY_VIOLATION", "snapshot datetime must be timezone-aware")
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise PlatformError("INTEGRITY_VIOLATION", "snapshot keys must be strings")
        return {key: _json_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise PlatformError("INTEGRITY_VIOLATION", "snapshot contains an unsupported value")


def _record_payload(value: object) -> dict[str, JsonValue]:
    payload = _json_value(value)
    if not isinstance(payload, dict):
        raise PlatformError("INTEGRITY_VIOLATION", "idempotency snapshot must be an object")
    return payload


def _snapshot_datetime(value: JsonValue) -> datetime:
    if not isinstance(value, str):
        raise TypeError("snapshot datetime is invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("snapshot datetime must be timezone-aware")
    return parsed


def _snapshot_optional_datetime(value: JsonValue) -> datetime | None:
    return None if value is None else _snapshot_datetime(value)


def _snapshot_optional_str(value: JsonValue) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError("snapshot optional string is invalid")


def _snapshot_str(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise TypeError("snapshot string is invalid")
    return value


def _snapshot_int(value: JsonValue) -> int:
    if type(value) is not int:
        raise TypeError("snapshot integer is invalid")
    return value


def _completed_payload(
    record: IdempotencyRecord,
    *,
    result_type: str,
    result_schema: str,
) -> dict[str, JsonValue]:
    if (
        record.status != "COMPLETED"
        or record.result_type != result_type
        or record.result_schema != result_schema
        or record.result_id is None
        or record.result_payload is None
    ):
        raise PlatformError("INTEGRITY_VIOLATION", "stored idempotency snapshot is invalid")
    return record.result_payload


def _run_from_idempotency(ctx: RequestContext, record: IdempotencyRecord) -> RunRecord:
    payload = _completed_payload(
        record,
        result_type="run",
        result_schema="run-record/v1",
    )
    try:
        resource_refs = payload["resource_refs"]
        parameters = payload["parameters"]
        if not isinstance(resource_refs, list) or not all(
            isinstance(item, str) for item in resource_refs
        ):
            raise TypeError("resource_refs are invalid")
        if not isinstance(parameters, dict):
            raise TypeError("parameters are invalid")
        run = RunRecord(
            tenant_id=_snapshot_str(payload["tenant_id"]),
            run_id=_snapshot_str(payload["run_id"]),
            owner_id=_snapshot_str(payload["owner_id"]),
            parent_run_id=_snapshot_optional_str(payload["parent_run_id"]),
            workflow_type=_snapshot_str(payload["workflow_type"]),
            intent=_snapshot_str(payload["intent"]),
            resource_refs=tuple(resource_refs),
            parameters=cast(dict[str, JsonValue], parameters),
            host_context_ref=_snapshot_optional_str(payload["host_context_ref"]),
            status=RunState(_snapshot_str(payload["status"])),
            status_reason=_snapshot_optional_str(payload["status_reason"]),
            version=_snapshot_int(payload["version"]),
            last_event_seq=_snapshot_int(payload["last_event_seq"]),
            fsm_version=_snapshot_str(payload["fsm_version"]),
            cancel_requested_by=_snapshot_optional_str(payload["cancel_requested_by"]),
            cancel_requested_at=_snapshot_optional_datetime(payload["cancel_requested_at"]),
            cancel_reason=_snapshot_optional_str(payload["cancel_reason"]),
            created_at=_snapshot_datetime(payload["created_at"]),
            updated_at=_snapshot_datetime(payload["updated_at"]),
            ended_at=_snapshot_optional_datetime(payload["ended_at"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PlatformError("INTEGRITY_VIOLATION", "stored Run snapshot is invalid") from error
    if run.tenant_id != ctx.tenant_id or run.run_id != record.result_id:
        raise PlatformError("INTEGRITY_VIOLATION", "stored Run snapshot identity is invalid")
    return run


def _reservation_from_idempotency(
    ctx: RequestContext, record: IdempotencyRecord
) -> AttemptReservation:
    payload = _completed_payload(
        record,
        result_type="attempt_reservation",
        result_schema="attempt-reservation/v1",
    )
    try:
        attempt_payload = payload["attempt"]
        lease_payload = payload["lease"]
        if not isinstance(attempt_payload, dict) or not isinstance(lease_payload, dict):
            raise TypeError("reservation records are invalid")
        attempt = AttemptRecord(
            tenant_id=_snapshot_str(attempt_payload["tenant_id"]),
            attempt_id=_snapshot_str(attempt_payload["attempt_id"]),
            run_id=_snapshot_str(attempt_payload["run_id"]),
            execution_unit_id=_snapshot_str(attempt_payload["execution_unit_id"]),
            step_id=_snapshot_optional_str(attempt_payload["step_id"]),
            generation=_snapshot_int(attempt_payload["generation"]),
            status=AttemptState(_snapshot_str(attempt_payload["status"])),
            version=_snapshot_int(attempt_payload["version"]),
            runtime_profile=_snapshot_str(attempt_payload["runtime_profile"]),
            source_checkpoint_id=_snapshot_optional_str(attempt_payload["source_checkpoint_id"]),
            reservation_key=_snapshot_str(attempt_payload["reservation_key"]),
            created_at=_snapshot_datetime(attempt_payload["created_at"]),
            updated_at=_snapshot_datetime(attempt_payload["updated_at"]),
            started_at=_snapshot_optional_datetime(attempt_payload["started_at"]),
            ended_at=_snapshot_optional_datetime(attempt_payload["ended_at"]),
            failure_id=_snapshot_optional_str(attempt_payload["failure_id"]),
        )
        lease = ExecutionLeaseRecord(
            tenant_id=_snapshot_str(lease_payload["tenant_id"]),
            lease_id=_snapshot_str(lease_payload["lease_id"]),
            run_id=_snapshot_str(lease_payload["run_id"]),
            execution_unit_id=_snapshot_str(lease_payload["execution_unit_id"]),
            attempt_id=_snapshot_str(lease_payload["attempt_id"]),
            generation=_snapshot_int(lease_payload["generation"]),
            state=ExecutionLeaseState(_snapshot_str(lease_payload["state"])),
            owner=_snapshot_optional_str(lease_payload["owner"]),
            version=_snapshot_int(lease_payload["version"]),
            activated_from_version=(
                None
                if lease_payload["activated_from_version"] is None
                else _snapshot_int(lease_payload["activated_from_version"])
            ),
            provision_deadline=_snapshot_datetime(lease_payload["provision_deadline"]),
            heartbeat_at=_snapshot_optional_datetime(lease_payload["heartbeat_at"]),
            expires_at=_snapshot_optional_datetime(lease_payload["expires_at"]),
            released_at=_snapshot_optional_datetime(lease_payload["released_at"]),
            created_at=_snapshot_datetime(lease_payload["created_at"]),
            updated_at=_snapshot_datetime(lease_payload["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PlatformError(
            "INTEGRITY_VIOLATION", "stored Attempt reservation snapshot is invalid"
        ) from error
    if (
        attempt.tenant_id != ctx.tenant_id
        or lease.tenant_id != ctx.tenant_id
        or attempt.attempt_id != record.result_id
        or lease.attempt_id != attempt.attempt_id
    ):
        raise PlatformError("INTEGRITY_VIOLATION", "stored Attempt reservation identity is invalid")
    return AttemptReservation(attempt=attempt, lease=lease)


class ControlPlaneService:
    def __init__(
        self,
        store: PlatformStore,
        runtime_profile: str = "business-analysis",
        fsm_version: str = "fsm/v1",
        provision_window: timedelta = timedelta(minutes=10),
        lease_ttl: timedelta = timedelta(minutes=2),
    ) -> None:
        if provision_window <= timedelta(0) or lease_ttl <= timedelta(0):
            raise ValueError("provision_window and lease_ttl must be positive")
        self._store = store
        self._runtime_profile = runtime_profile
        self._fsm_version = fsm_version
        self._provision_window = provision_window
        self._lease_ttl = lease_ttl

    async def create_run(
        self,
        ctx: RequestContext,
        command: CreateRunCommand,
        idempotency_key: str,
        authorization: RunAuthorizationContext | None = None,
    ) -> RunRecord:
        if not idempotency_key:
            raise PlatformError("INTEGRITY_VIOLATION", "idempotency key is required")
        digest_payload: object = command
        if authorization is not None:
            digest_payload = {"authorization": authorization, "command": command}
        digest = _request_digest(ctx, "create_run", digest_payload)
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            existing = await tx.claim_idempotency(
                ctx.tenant_id,
                "create_run",
                idempotency_key,
                digest,
                ctx.actor_id,
                now,
            )
            if existing is not None:
                return _run_from_idempotency(ctx, existing)
            run = await self._insert_initial_run(
                tx,
                ctx,
                command,
                parent_run_id=None,
                authorization=authorization,
            )
            await tx.complete_idempotency(
                ctx.tenant_id,
                "create_run",
                idempotency_key,
                digest,
                "run",
                run.run_id,
                "run-record/v1",
                _record_payload(run),
                now,
            )
            return run

    async def reserve_attempt(
        self,
        ctx: RequestContext,
        execution_unit_id: str,
        source_checkpoint_id: str,
        expected_unit_version: int,
        *,
        transition_key: str,
    ) -> AttemptReservation:
        if not transition_key:
            raise PlatformError("INTEGRITY_VIOLATION", "transition key is required")
        digest = _request_digest(
            ctx,
            "reserve_attempt",
            {
                "execution_unit_id": execution_unit_id,
                "source_checkpoint_id": source_checkpoint_id,
                "expected_unit_version": expected_unit_version,
            },
        )
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            existing = await tx.claim_idempotency(
                ctx.tenant_id,
                "reserve_attempt",
                transition_key,
                digest,
                ctx.actor_id,
                now,
            )
            if existing is not None:
                return _reservation_from_idempotency(ctx, existing)
            candidate_unit = await tx.get_execution_unit(ctx.tenant_id, execution_unit_id)
            run = await tx.lock_run(ctx.tenant_id, candidate_unit.run_id)
            unit = await tx.lock_execution_unit(ctx.tenant_id, execution_unit_id)
            if unit.version != expected_unit_version:
                raise PlatformError(
                    "VERSION_CONFLICT", "execution unit version compare-and-swap failed"
                )
            if run.cancel_requested_at is not None or run.status is RunState.CANCEL_REQUESTED:
                raise PlatformError("INVALID_STATE", "cancelled run cannot reserve an Attempt")
            if run.status not in {RunState.QUEUED, RunState.RUNNING, RunState.RECOVERING}:
                raise PlatformError("INVALID_STATE", "run is not schedulable")
            if unit.status not in {
                ExecutionUnitState.DISPATCHABLE,
                ExecutionUnitState.RECOVERING,
            }:
                raise PlatformError("INVALID_STATE", "execution unit is not schedulable")
            try:
                checkpoint = await tx.get_checkpoint(ctx.tenant_id, source_checkpoint_id)
            except PlatformError as error:
                if error.code != "NOT_FOUND":
                    raise
                raise PlatformError("SOURCE_CHECKPOINT_INVALID", "source checkpoint is not valid") from error
            if (
                checkpoint.run_id != run.run_id
                or checkpoint.execution_unit_id != unit.execution_unit_id
                or checkpoint.state is not CheckpointState.COMMITTED
                or checkpoint.checkpoint_id != unit.current_checkpoint_id
            ):
                raise PlatformError(
                    "SOURCE_CHECKPOINT_INVALID",
                    "source checkpoint must be the unit's current committed cursor",
                )
            active_attempts = await tx.list_active_attempts_for_run(ctx.tenant_id, run.run_id)
            if active_attempts:
                raise PlatformError("ACTIVE_ATTEMPT_EXISTS", "one active Attempt per Run")
            active_leases = await tx.list_active_leases_for_run(ctx.tenant_id, run.run_id)
            if active_leases:
                raise PlatformError("ACTIVE_EXECUTION_EXISTS", "one active execution per Run")
            units = await tx.list_execution_units_for_run(ctx.tenant_id, run.run_id)
            if any(
                item.execution_unit_id != unit.execution_unit_id
                and item.status in {ExecutionUnitState.EXECUTING, ExecutionUnitState.RECOVERING}
                for item in units
            ):
                raise PlatformError(
                    "ACTIVE_EXECUTION_EXISTS", "another execution unit is active for this Run"
                )
            generation = unit.next_generation + 1
            attempt = AttemptRecord(
                tenant_id=ctx.tenant_id,
                attempt_id=self._store.new_id("attempt"),
                run_id=run.run_id,
                execution_unit_id=unit.execution_unit_id,
                step_id=None,
                generation=generation,
                status=AttemptState.PROVISIONING,
                version=1,
                runtime_profile=unit.runtime_profile,
                source_checkpoint_id=checkpoint.checkpoint_id,
                reservation_key=transition_key,
                created_at=now,
                updated_at=now,
                started_at=None,
                ended_at=None,
                failure_id=None,
            )
            lease = ExecutionLeaseRecord(
                tenant_id=ctx.tenant_id,
                lease_id=self._store.new_id("lease"),
                run_id=run.run_id,
                execution_unit_id=unit.execution_unit_id,
                attempt_id=attempt.attempt_id,
                generation=generation,
                state=ExecutionLeaseState.RESERVED,
                owner=None,
                version=1,
                activated_from_version=None,
                provision_deadline=now + self._provision_window,
                heartbeat_at=None,
                expires_at=None,
                released_at=None,
                created_at=now,
                updated_at=now,
            )
            updated_unit = replace(
                unit,
                next_generation=generation,
                version=unit.version + 1,
                updated_at=now,
            )
            event_id = self._store.new_id("event")
            event = self._attempt_event(ctx, run, attempt, event_id, now, run.last_event_seq + 1)
            updated_run = replace(
                run,
                version=run.version + 1,
                last_event_seq=event.event_seq,
                updated_at=now,
            )
            outbox = OutboxMessageRecord(
                tenant_id=ctx.tenant_id,
                message_id=self._store.new_id("outbox"),
                run_id=run.run_id,
                topic="attempt.provisioning.requested",
                payload={
                    "attempt_id": attempt.attempt_id,
                    "execution_unit_id": unit.execution_unit_id,
                    "generation": generation,
                    "source_checkpoint_id": checkpoint.checkpoint_id,
                },
                event_id=event_id,
                aggregate_version=updated_run.version,
                created_at=now,
                published_at=None,
            )
            await tx.insert_attempt(attempt)
            await tx.insert_lease(lease)
            await tx.replace_execution_unit_cas(updated_unit, expected_unit_version)
            await tx.replace_run_cas(updated_run, run.version)
            await tx.append_event(event, expected_previous_seq=run.last_event_seq)
            await tx.insert_outbox(outbox)
            await tx.complete_idempotency(
                ctx.tenant_id,
                "reserve_attempt",
                transition_key,
                digest,
                "attempt_reservation",
                attempt.attempt_id,
                "attempt-reservation/v1",
                _record_payload(AttemptReservation(attempt=attempt, lease=lease)),
                now,
            )
            return AttemptReservation(attempt=attempt, lease=lease)

    async def activate_lease(
        self,
        ctx: RequestContext,
        attempt_id: str,
        generation: int,
        owner: str,
        expected_lease_version: int,
    ) -> ExecutionLeaseRecord:
        if not owner:
            raise PlatformError("LEASE_OWNER_MISMATCH", "lease owner is required")
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            attempt = await tx.get_attempt(ctx.tenant_id, attempt_id)
            run = await tx.lock_run(ctx.tenant_id, attempt.run_id)
            unit = await tx.lock_execution_unit(ctx.tenant_id, attempt.execution_unit_id)
            lease = await tx.get_lease_for_attempt(ctx.tenant_id, attempt_id)
            self._validate_attempt_relations(run, unit, attempt, lease)
            self._validate_generation(unit, attempt, lease, generation)
            if run.cancel_requested_at is not None or run.status is RunState.CANCEL_REQUESTED:
                raise PlatformError("INVALID_STATE", "cancelled run cannot activate a Lease")
            if lease.state is ExecutionLeaseState.ACTIVE:
                if lease.owner != owner:
                    raise PlatformError("LEASE_OWNER_MISMATCH", "lease is owned by another runtime")
                if (
                    lease.activated_from_version is None
                    or expected_lease_version != lease.activated_from_version
                    or lease.version != lease.activated_from_version + 1
                ):
                    raise PlatformError(
                        "VERSION_CONFLICT", "activation replay provenance does not match"
                    )
                if lease.expires_at is None:
                    raise PlatformError("INTEGRITY_VIOLATION", "active lease has no expiry")
                if now >= lease.expires_at:
                    raise PlatformError("LEASE_EXPIRED", "active lease has expired")
                if (
                    attempt.status not in (AttemptState.CLAIMED, AttemptState.RUNNING)
                    or unit.status is not ExecutionUnitState.EXECUTING
                ):
                    raise PlatformError("INTEGRITY_VIOLATION", "active lease facts are inconsistent")
                return lease
            if lease.state is not ExecutionLeaseState.RESERVED:
                raise PlatformError("LEASE_NOT_ACTIVE", "lease is not reservable")
            if lease.version != expected_lease_version:
                raise PlatformError("VERSION_CONFLICT", "lease version compare-and-swap failed")
            if now >= lease.provision_deadline:
                raise PlatformError("LEASE_EXPIRED", "lease provision deadline has expired")
            if attempt.status is not AttemptState.PROVISIONING:
                raise PlatformError("INVALID_STATE", "Attempt is not provisioning")
            if unit.status not in {
                ExecutionUnitState.DISPATCHABLE,
                ExecutionUnitState.RECOVERING,
            }:
                raise PlatformError("INVALID_STATE", "execution unit cannot activate")
            if run.status not in {RunState.QUEUED, RunState.RUNNING, RunState.RECOVERING}:
                raise PlatformError("INVALID_STATE", "run cannot activate")
            active_lease = replace(
                lease,
                state=ExecutionLeaseState.ACTIVE,
                owner=owner,
                version=lease.version + 1,
                activated_from_version=lease.version,
                heartbeat_at=now,
                expires_at=now + self._lease_ttl,
                updated_at=now,
            )
            claimed_attempt = replace(
                attempt,
                status=AttemptState.CLAIMED,
                version=attempt.version + 1,
                updated_at=now,
                started_at=now,
            )
            executing_unit = replace(
                unit,
                status=ExecutionUnitState.EXECUTING,
                version=unit.version + 1,
                updated_at=now,
            )
            event_specs: list[tuple[EventType, object, str, str]] = [
                (
                    EventType.ATTEMPT_LIFECYCLE,
                    AttemptLifecyclePayload(
                        kind="attempt.lifecycle",
                        attempt_id=attempt.attempt_id,
                        status=AttemptState.CLAIMED,
                    ),
                    "attempt-lifecycle/v1",
                    self._store.new_id("event"),
                ),
            ]
            next_run_status = (
                RunState.RUNNING
                if run.status in (RunState.QUEUED, RunState.RECOVERING)
                else run.status
            )
            if next_run_status is not run.status:
                event_specs.append(
                    (
                        EventType.RUN_STATUS_CHANGED,
                        RunStatusChangedPayload(
                            kind="run.status.changed",
                            previous=run.status,
                            current=next_run_status,
                        ),
                        "run-status/v1",
                        self._store.new_id("event"),
                    )
                )
            events = self._events_from_specs(ctx, run, now, event_specs, attempt.attempt_id)
            executing_run = replace(
                run,
                status=next_run_status,
                status_reason=None,
                version=run.version + 1,
                last_event_seq=run.last_event_seq + len(events),
                updated_at=now,
            )
            outbox = OutboxMessageRecord(
                tenant_id=ctx.tenant_id,
                message_id=self._store.new_id("outbox"),
                run_id=run.run_id,
                topic="attempt.activated",
                payload={
                    "attempt_id": attempt.attempt_id,
                    "execution_unit_id": unit.execution_unit_id,
                    "generation": generation,
                },
                event_id=events[0].event_id,
                aggregate_version=executing_run.version,
                created_at=now,
                published_at=None,
            )
            await tx.replace_lease_cas(active_lease, lease.version)
            await tx.replace_attempt_cas(claimed_attempt, attempt.version)
            await tx.replace_execution_unit_cas(executing_unit, unit.version)
            await tx.replace_run_cas(executing_run, run.version)
            previous_seq = run.last_event_seq
            for event in events:
                await tx.append_event(event, expected_previous_seq=previous_seq)
                previous_seq = event.event_seq
            await tx.insert_outbox(outbox)
            return active_lease

    async def renew_lease(
        self,
        ctx: RequestContext,
        attempt_id: str,
        generation: int,
        owner: str,
        expected_lease_version: int,
    ) -> ExecutionLeaseRecord:
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            attempt = await tx.get_attempt(ctx.tenant_id, attempt_id)
            run = await tx.lock_run(ctx.tenant_id, attempt.run_id)
            unit = await tx.lock_execution_unit(ctx.tenant_id, attempt.execution_unit_id)
            lease = await tx.get_lease_for_attempt(ctx.tenant_id, attempt_id)
            self._validate_attempt_relations(run, unit, attempt, lease)
            self._validate_generation(unit, attempt, lease, generation)
            if run.cancel_requested_at is not None or run.status is RunState.CANCEL_REQUESTED:
                raise PlatformError("INVALID_STATE", "cancelled run cannot renew a Lease")
            if lease.state is not ExecutionLeaseState.ACTIVE:
                raise PlatformError("LEASE_NOT_ACTIVE", "lease is not active")
            if lease.owner != owner:
                raise PlatformError("LEASE_OWNER_MISMATCH", "lease is owned by another runtime")
            if lease.version != expected_lease_version:
                raise PlatformError("VERSION_CONFLICT", "lease version compare-and-swap failed")
            if lease.expires_at is None:
                raise PlatformError("INTEGRITY_VIOLATION", "active lease has no expiry")
            if now >= lease.expires_at:
                raise PlatformError("LEASE_EXPIRED", "active lease has expired")
            renewed = replace(
                lease,
                version=lease.version + 1,
                heartbeat_at=now,
                expires_at=now + self._lease_ttl,
                updated_at=now,
            )
            await tx.replace_lease_cas(renewed, lease.version)
            return renewed

    async def request_cancel(
        self,
        ctx: RequestContext,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        reason: str | None = None,
    ) -> RunRecord:
        if not idempotency_key:
            raise PlatformError("INTEGRITY_VIOLATION", "idempotency key is required")
        digest = _request_digest(
            ctx,
            "request_cancel",
            {
                "run_id": run_id,
                "expected_run_version": expected_run_version,
                "reason": reason,
            },
        )
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            existing = await tx.claim_idempotency(
                ctx.tenant_id,
                "request_cancel",
                idempotency_key,
                digest,
                ctx.actor_id,
                now,
            )
            if existing is not None:
                return _run_from_idempotency(ctx, existing)
            run = await tx.lock_run(ctx.tenant_id, run_id)
            if run.version != expected_run_version:
                raise PlatformError("VERSION_CONFLICT", "run version compare-and-swap failed")
            if run.status in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
                raise PlatformError("INVALID_STATE", "terminal run cannot be cancelled")
            if run.status is RunState.CANCEL_REQUESTED:
                raise PlatformError("INVALID_STATE", "cancellation was already requested")
            event_id = self._store.new_id("event")
            event = EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=event_id,
                tenant_id=ctx.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + 1,
                event_type=EventType.RUN_STATUS_CHANGED,
                occurred_at=now,
                producer_service="control-plane",
                payload_schema="run-status/v1",
                payload=RunStatusChangedPayload(
                    kind="run.status.changed",
                    previous=run.status,
                    current=RunState.CANCEL_REQUESTED,
                ),
                trace_id=ctx.trace_id,
            )
            cancelled = replace(
                run,
                status=RunState.CANCEL_REQUESTED,
                status_reason="CANCEL_REQUESTED",
                version=run.version + 1,
                last_event_seq=event.event_seq,
                cancel_requested_by=ctx.actor_id,
                cancel_requested_at=now,
                cancel_reason=reason,
                updated_at=now,
            )
            audit = AuditEventRecord(
                tenant_id=ctx.tenant_id,
                audit_event_id=self._store.new_id("audit"),
                run_id=run.run_id,
                actor_id=ctx.actor_id,
                action="run.cancel.requested",
                entity_type="run",
                entity_id=run.run_id,
                entity_version=cancelled.version,
                outcome="ACCEPTED",
                trace_id=ctx.trace_id,
                details={"reason": reason} if reason is not None else {},
                created_at=now,
            )
            outbox = OutboxMessageRecord(
                tenant_id=ctx.tenant_id,
                message_id=self._store.new_id("outbox"),
                run_id=run.run_id,
                topic="execution.cleanup.requested",
                payload={"run_id": run.run_id},
                event_id=event_id,
                aggregate_version=cancelled.version,
                created_at=now,
                published_at=None,
            )
            await tx.replace_run_cas(cancelled, run.version)
            await tx.append_event(event, run.last_event_seq)
            await tx.insert_audit(audit)
            await tx.insert_outbox(outbox)
            await tx.complete_idempotency(
                ctx.tenant_id,
                "request_cancel",
                idempotency_key,
                digest,
                "run",
                run.run_id,
                "run-record/v1",
                _record_payload(cancelled),
                now,
            )
            return cancelled

    async def rerun(
        self,
        ctx: RequestContext,
        parent_run_id: str,
        *,
        idempotency_key: str,
        expected_parent_version: int | None = None,
    ) -> RunRecord:
        if not idempotency_key:
            raise PlatformError("INTEGRITY_VIOLATION", "idempotency key is required")
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            await tx.get_run(ctx.tenant_id, parent_run_id)
            try:
                parent_authorization = await tx.get_run_authorization_snapshot(
                    ctx.tenant_id, parent_run_id
                )
            except PlatformError as error:
                if error.code != "NOT_FOUND":
                    raise
                parent_authorization = None
            digest = _request_digest(
                ctx,
                "rerun",
                {
                    "authorization_snapshot_digest": (
                        parent_authorization.snapshot_digest
                        if parent_authorization is not None
                        else None
                    ),
                    "expected_parent_version": expected_parent_version,
                    "parent_run_id": parent_run_id,
                },
            )
            existing = await tx.claim_idempotency(
                ctx.tenant_id,
                "rerun",
                idempotency_key,
                digest,
                ctx.actor_id,
                now,
            )
            if existing is not None:
                return _run_from_idempotency(ctx, existing)
            parent = await tx.lock_run(ctx.tenant_id, parent_run_id)
            if expected_parent_version is not None and parent.version != expected_parent_version:
                raise PlatformError("VERSION_CONFLICT", "parent Run version has changed")
            if parent.status not in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
                raise PlatformError("INVALID_STATE", "only terminal Runs can be rerun")
            command = CreateRunCommand(
                workflow_type=parent.workflow_type,
                intent=parent.intent,
                resource_refs=parent.resource_refs,
                parameters=parent.parameters,
                host_context_ref=parent.host_context_ref,
            )
            child = await self._insert_initial_run(
                tx,
                ctx,
                command,
                parent_run_id=parent.run_id,
                authorization=(
                    None
                    if parent_authorization is None
                    else RunAuthorizationContext(
                        resolved_resources=parent_authorization.resolved_resources,
                        host_context_digest=parent_authorization.host_context_digest,
                        host_context_version=parent_authorization.host_context_version,
                        policy_digest=parent_authorization.policy_digest,
                        policy_version=parent_authorization.policy_version,
                        policy_scopes=parent_authorization.policy_scopes,
                        policy_budget=parent_authorization.policy_budget,
                        snapshot_digest=parent_authorization.snapshot_digest,
                    )
                ),
            )
            await tx.complete_idempotency(
                ctx.tenant_id,
                "rerun",
                idempotency_key,
                digest,
                "run",
                child.run_id,
                "run-record/v1",
                _record_payload(child),
                now,
            )
            return child

    async def queue_followup(
        self,
        ctx: RequestContext,
        run_id: str,
        *,
        question: str,
        client_followup_id: str,
    ) -> FollowupRequestRecord:
        """Reactivate a terminal Run for a follow-up Attempt and queue the question.

        Transitions the Run and its primary ExecutionUnit from a terminal state
        (SUCCEEDED/FAILED) back into RECOVERING, so the next Scheduler polling
        cycle claims the unit again and reserves a new Attempt (generation+1).
        The durable FollowupRequestRecord carries the question; the parent
        orchestrator injects it into the new child Runner via ``_op_restore``
        and writes the answer back when the Attempt commits.
        """
        if not question.strip():
            raise PlatformError("INTEGRITY_VIOLATION", "follow-up question must not be empty")
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            run = await tx.lock_run(ctx.tenant_id, run_id)
            if run.status in (RunState.CANCELLED, RunState.CANCEL_REQUESTED):
                raise PlatformError("INVALID_STATE", "cancelled runs cannot accept follow-ups")
            unit = await tx.get_primary_unit(ctx.tenant_id, run_id)
            unit = await tx.lock_execution_unit(ctx.tenant_id, unit.execution_unit_id)

            # Reactivate terminal aggregates so the scheduler can claim them again.
            if run.status in (RunState.SUCCEEDED, RunState.FAILED):
                _fsm(EntityType.RUN, run.status, RunState.RECOVERING, None)
                reactivated_run = replace(
                    run,
                    status=RunState.RECOVERING,
                    status_reason=None,
                    version=run.version + 1,
                    updated_at=now,
                )
                await tx.replace_run_cas(reactivated_run, run.version)
                run = reactivated_run
            if unit.status in (ExecutionUnitState.SUCCEEDED, ExecutionUnitState.FAILED):
                _fsm(
                    EntityType.EXECUTION_UNIT,
                    unit.status,
                    ExecutionUnitState.RECOVERING,
                    None,
                )
                reactivated_unit = replace(
                    unit,
                    status=ExecutionUnitState.RECOVERING,
                    version=unit.version + 1,
                    updated_at=now,
                )
                await tx.replace_execution_unit_cas(reactivated_unit, unit.version)
                unit = reactivated_unit

            followup = FollowupRequestRecord(
                tenant_id=ctx.tenant_id,
                followup_id=self._store.new_id("followup"),
                run_id=run.run_id,
                question=question,
                client_followup_id=client_followup_id,
                status="PENDING",
                answer=None,
                version=1,
                created_at=now,
                answered_at=None,
            )
            await tx.insert_followup_request(followup)
            return followup

    async def _insert_initial_run(
        self,
        tx: PlatformTransaction,
        ctx: RequestContext,
        command: CreateRunCommand,
        *,
        parent_run_id: str | None,
        authorization: RunAuthorizationContext | None,
    ) -> RunRecord:
        now = await tx.db_now()
        run_id = self._store.new_id("run")
        unit_id = self._store.new_id("unit")
        checkpoint_id = self._store.new_id("checkpoint")
        event_id = self._store.new_id("event")
        run = RunRecord(
            tenant_id=ctx.tenant_id,
            run_id=run_id,
            owner_id=ctx.actor_id,
            parent_run_id=parent_run_id,
            workflow_type=command.workflow_type,
            intent=command.intent,
            resource_refs=tuple(command.resource_refs),
            parameters=dict(command.parameters),
            host_context_ref=command.host_context_ref,
            status=RunState.QUEUED,
            status_reason=None,
            version=1,
            last_event_seq=1,
            fsm_version=self._fsm_version,
            cancel_requested_by=None,
            cancel_requested_at=None,
            cancel_reason=None,
            created_at=now,
            updated_at=now,
            ended_at=None,
        )
        authorization_snapshot = (
            None
            if authorization is None
            else RunAuthorizationSnapshotRecord(
                tenant_id=ctx.tenant_id,
                run_id=run_id,
                resolved_resources=authorization.resolved_resources,
                host_context_digest=authorization.host_context_digest,
                host_context_version=authorization.host_context_version,
                policy_digest=authorization.policy_digest,
                policy_version=authorization.policy_version,
                policy_scopes=authorization.policy_scopes,
                policy_budget=authorization.policy_budget,
                snapshot_digest=authorization.snapshot_digest,
                created_at=now,
            )
        )
        unit = ExecutionUnitRecord(
            tenant_id=ctx.tenant_id,
            execution_unit_id=unit_id,
            run_id=run_id,
            role="primary",
            status=ExecutionUnitState.DISPATCHABLE,
            version=1,
            current_checkpoint_id=checkpoint_id,
            next_generation=0,
            runtime_profile=self._runtime_profile,
            created_at=now,
            updated_at=now,
        )
        checkpoint = CheckpointRecord(
            tenant_id=ctx.tenant_id,
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            execution_unit_id=unit_id,
            source_attempt_id=None,
            checkpoint_seq=0,
            state=CheckpointState.COMMITTED,
            workflow_cursor={},
            last_event_seq=0,
            workspace_snapshot_id=None,
            checkpoint_schema_version="checkpoint/v1",
            runtime_profile_version=self._runtime_profile,
            policy_version="policy/v1",
            tool_catalog_version="tool-catalog/v1",
            ui_catalog_version="ui-catalog/v1",
            checksum=hashlib.sha256(b"{}").hexdigest(),
            version=1,
            created_at=now,
            committed_at=now,
            completed_step_ids=(),
        )
        event = EnterpriseEventEnvelope(
            schema_version="enterprise-event/v1",
            event_id=event_id,
            tenant_id=ctx.tenant_id,
            run_id=run_id,
            event_seq=1,
            event_type=EventType.RUN_CREATED,
            occurred_at=now,
            producer_service="control-plane",
            payload_schema="run-created/v1",
            payload=RunCreatedPayload(
                kind="run.created",
                workflow_type=command.workflow_type,
            ),
            trace_id=ctx.trace_id,
        )
        outbox = OutboxMessageRecord(
            tenant_id=ctx.tenant_id,
            message_id=self._store.new_id("outbox"),
            run_id=run_id,
            topic="dispatch.requested",
            payload={"run_id": run_id, "execution_unit_id": unit_id},
            event_id=event_id,
            aggregate_version=run.version,
            created_at=now,
            published_at=None,
        )
        audit = (
            None
            if authorization is None
            else AuditEventRecord(
                tenant_id=ctx.tenant_id,
                audit_event_id=self._store.new_id("audit"),
                run_id=run_id,
                actor_id=ctx.actor_id,
                action="run.created",
                entity_type="run",
                entity_id=run_id,
                entity_version=run.version,
                outcome="ACCEPTED",
                trace_id=ctx.trace_id,
                details={"authorization_snapshot_digest": authorization.snapshot_digest},
                created_at=now,
            )
        )
        await tx.insert_run(run)
        if authorization_snapshot is not None:
            await tx.insert_run_authorization_snapshot(authorization_snapshot)
        await tx.insert_execution_unit(unit)
        await tx.insert_checkpoint(checkpoint)
        await tx.append_event(event, expected_previous_seq=0)
        if audit is not None:
            await tx.insert_audit(audit)
        await tx.insert_outbox(outbox)
        return run

    def _attempt_event(
        self,
        ctx: RequestContext,
        run: RunRecord,
        attempt: AttemptRecord,
        event_id: str,
        now: datetime,
        event_seq: int,
    ) -> EnterpriseEventEnvelope:
        return EnterpriseEventEnvelope(
            schema_version="enterprise-event/v1",
            event_id=event_id,
            tenant_id=ctx.tenant_id,
            run_id=run.run_id,
            event_seq=event_seq,
            event_type=EventType.ATTEMPT_LIFECYCLE,
            occurred_at=now,
            producer_service="control-plane",
            payload_schema="attempt-lifecycle/v1",
            payload=AttemptLifecyclePayload(
                kind="attempt.lifecycle",
                attempt_id=attempt.attempt_id,
                status=attempt.status,
            ),
            attempt_id=attempt.attempt_id,
            trace_id=ctx.trace_id,
        )

    def _events_from_specs(
        self,
        ctx: RequestContext,
        run: RunRecord,
        now: datetime,
        specs: list[tuple[EventType, object, str, str]],
        attempt_id: str,
    ) -> tuple[EnterpriseEventEnvelope, ...]:
        return tuple(
            EnterpriseEventEnvelope(
                schema_version="enterprise-event/v1",
                event_id=event_id,
                tenant_id=ctx.tenant_id,
                run_id=run.run_id,
                event_seq=run.last_event_seq + offset,
                event_type=event_type,
                occurred_at=now,
                producer_service="control-plane",
                payload_schema=payload_schema,
                payload=payload,
                attempt_id=attempt_id,
                trace_id=ctx.trace_id,
            )
            for offset, (event_type, payload, payload_schema, event_id) in enumerate(specs, 1)
        )

    @staticmethod
    def _validate_attempt_relations(
        run: RunRecord,
        unit: ExecutionUnitRecord,
        attempt: AttemptRecord,
        lease: ExecutionLeaseRecord,
    ) -> None:
        if (
            attempt.run_id != run.run_id
            or attempt.execution_unit_id != unit.execution_unit_id
            or unit.run_id != run.run_id
            or lease.run_id != run.run_id
            or lease.execution_unit_id != unit.execution_unit_id
            or lease.attempt_id != attempt.attempt_id
        ):
            raise PlatformError("INTEGRITY_VIOLATION", "Attempt/Lease relations are invalid")

    @staticmethod
    def _validate_generation(
        unit: ExecutionUnitRecord,
        attempt: AttemptRecord,
        lease: ExecutionLeaseRecord,
        generation: int,
    ) -> None:
        if (
            generation != unit.next_generation
            or generation != attempt.generation
            or generation != lease.generation
        ):
            raise PlatformError("STALE_GENERATION", "generation fence is stale")
