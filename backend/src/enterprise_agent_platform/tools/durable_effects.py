"""Durable Effect executor and reconciliation.

The executor claims a PREPARED Effect inside a transaction, calls the external
Connector outside the transaction, and closes the ledger with a terminal state
inside a second transaction. A crash or unknown outcome leaves the Effect in
EXECUTING/UNKNOWN; a reconciliation flow (with its own capability token) then
decides the terminal state. Every transition appends an effect.status.changed
event and advances the run's event sequence.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from enterprise_agent_platform.contracts.enums import EffectState, EventType
from enterprise_agent_platform.contracts.events import (
    EffectStatusChangedPayload,
    EnterpriseEventEnvelope,
)
from enterprise_agent_platform.domain.records import EffectLedgerRecord
from enterprise_agent_platform.persistence.protocol import PlatformError
from enterprise_agent_platform.security.capabilities import VerifiedEffectCapability
from enterprise_agent_platform.tools.connectors import (
    ConnectorCallContext,
    ConnectorKnownFailure,
    ConnectorOutcomeUnknown,
)

_EXECUTION_LEASE_SECONDS = 300


@dataclass(frozen=True, slots=True)
class ReconciledDurableEffect:
    succeeded: bool
    remote_operation_id: str | None
    result: dict[str, JsonValue]
    evidence_ref: str
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class ResolvedEffectPayload:
    arguments: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class VerifiedEffectReconciliation:
    actor_id: str
    executor_inactive: bool
    observation_stable: bool


def compute_reconciliation_evidence_digest(
    *,
    effect_id: str,
    effect_key: str,
    succeeded: bool,
    remote_operation_id: str | None,
    result: dict[str, JsonValue],
    evidence_ref: str,
) -> str:
    payload = json.dumps(
        {
            "effect_id": effect_id,
            "effect_key": effect_key,
            "succeeded": succeeded,
            "remote_operation_id": remote_operation_id,
            "result": result,
            "evidence_ref": evidence_ref,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class DurableEffectExecutor:
    def __init__(self, *, store, verifier, reconciliation_authorizer, payloads, broker, connectors):
        self._store = store
        self._verifier = verifier
        self._reconciliation_authorizer = reconciliation_authorizer
        self._payloads = payloads
        self._broker = broker
        self._connectors = connectors

    async def execute(self, tenant_id, effect_id, token, *, executor_id):
        effect = await self._store.get_effect(tenant_id, effect_id)
        capability = await self._verifier.verify_effect(
            token,
            tenant_id=tenant_id,
            effect_id=effect_id,
            approval_id=effect.approval_id,
            request_digest=effect.request_digest,
            required_scopes=effect.required_scopes,
        )
        if capability.effect_id != effect_id or capability.expires_at <= datetime.now(UTC):
            raise PlatformError(
                "CAPABILITY_MISMATCH", "effect capability does not match the Effect"
            )
        if effect.state is EffectState.SUCCEEDED or effect.state is EffectState.FAILED:
            return effect
        if effect.state is EffectState.UNKNOWN:
            raise PlatformError(
                "EFFECT_OUTCOME_UNKNOWN",
                "Effect outcome is unknown; reconcile before executing again",
            )
        proposal = await self._store.get_action_proposal(tenant_id, effect.action_ref)
        resolved = await self._payloads.resolve(
            tenant_id=tenant_id, payload_ref=proposal.payload_ref
        )
        credential = await self._broker.acquire(
            tenant_id=tenant_id,
            connector_name=effect.connector_name,
            resource_ref=effect.canonical_target,
        )
        connector = self._connectors.get(effect.connector_name)
        if connector is None:
            raise PlatformError(
                "CONNECTOR_NOT_FOUND", f"connector {effect.connector_name} is not configured"
            )
        async with self._store.transaction() as tx:
            now = await tx.db_now()
            run = await tx.lock_run(tenant_id, effect.run_id)
            current = await tx.get_effect(tenant_id, effect_id)
            if current.state is EffectState.SUCCEEDED or current.state is EffectState.FAILED:
                return current
            if current.state is not EffectState.PREPARED:
                raise PlatformError(
                    "EFFECT_ALREADY_EXECUTING", "Effect is not PREPARED for execution"
                )
            claimed = replace(
                current,
                state=EffectState.EXECUTING,
                executor_id=executor_id,
                execution_epoch=current.execution_epoch + 1,
                executor_lease_expires_at=now + timedelta(seconds=_EXECUTION_LEASE_SECONDS),
                version=current.version + 1,
                updated_at=now,
            )
            await tx.replace_effect_cas(claimed, current.version)
            await self._append_effect_event(
                tx,
                run,
                effect,
                EffectState.EXECUTING,
                now,
                token,
            )
        try:
            output = await connector.invoke(
                ConnectorCallContext(idempotency_key=effect.effect_key),
                operation=effect.tool_name,
                resource_ref=effect.canonical_target,
                arguments=dict(resolved.arguments),
                credential=credential,
            )
        except ConnectorKnownFailure as error:
            return await self._close_terminal(
                tenant_id,
                effect,
                EffectState.FAILED,
                error,
                token=token,
                result_ref=None,
                remote_operation_id=None,
            )
        except ConnectorOutcomeUnknown as error:
            return await self._close_terminal(
                tenant_id,
                effect,
                EffectState.UNKNOWN,
                error,
                token=token,
                result_ref=None,
                remote_operation_id=None,
            )
        if not isinstance(output, dict):
            raise PlatformError(
                "OUTPUT_INVALID", "connector output must be an object"
            )
        remote_id = output.get("remote_id")
        if not isinstance(remote_id, str):
            raise PlatformError("OUTPUT_INVALID", "connector output has no remote_id")
        result_ref = f"reference-defect:{remote_id}"
        return await self._close_terminal(
            tenant_id,
            effect,
            EffectState.SUCCEEDED,
            error=None,
            token=token,
            result_ref=result_ref,
            remote_operation_id=remote_id,
        )

    async def reconcile(self, tenant_id, effect_id, result, token):
        effect = await self._store.get_effect(tenant_id, effect_id)
        await self._reconciliation_authorizer.verify_reconciliation(
            token,
            tenant_id=tenant_id,
            effect_id=effect_id,
            effect_key=effect.effect_key,
            evidence_digest=result.evidence_digest,
        )
        target = EffectState.SUCCEEDED if result.succeeded else EffectState.FAILED
        return await self._close_terminal(
            tenant_id,
            effect,
            target,
            error=None,
            token=token,
            result_ref=result.evidence_ref,
            remote_operation_id=result.remote_operation_id,
            reconcile=True,
        )

    async def _close_terminal(
        self,
        tenant_id,
        effect,
        target: EffectState,
        error,
        *,
        token,
        result_ref,
        remote_operation_id,
        reconcile: bool = False,
    ) -> EffectLedgerRecord:
        async with self._store.transaction() as tx:
            tx_now = await tx.db_now()
            run = await tx.lock_run(tenant_id, effect.run_id)
            current = await tx.get_effect(tenant_id, effect.effect_id)
            if (
                current.state is EffectState.SUCCEEDED
                or current.state is EffectState.FAILED
            ):
                return current
            if current.state not in (EffectState.EXECUTING, EffectState.UNKNOWN):
                raise PlatformError(
                    "EFFECT_STATE_INVALID",
                    "Effect cannot close from its current state",
                )
            closed = replace(
                current,
                state=target,
                executor_lease_expires_at=None,
                result_ref=result_ref,
                remote_operation_id=remote_operation_id,
                completed_at=None if target is EffectState.UNKNOWN else tx_now,
                version=current.version + 1,
                updated_at=tx_now,
            )
            await tx.replace_effect_cas(closed, current.version)
            await self._append_effect_event(
                tx,
                run,
                effect,
                target,
                tx_now,
                token,
            )
            return closed

    async def _append_effect_event(
        self,
        tx,
        run,
        effect: EffectLedgerRecord,
        status: EffectState,
        now: datetime,
        token: str,
    ) -> None:
        event = EnterpriseEventEnvelope(
            schema_version="enterprise-event/v1",
            event_id=self._store.new_id("event"),
            tenant_id=effect.tenant_id,
            run_id=effect.run_id,
            event_seq=run.last_event_seq + 1,
            event_type=EventType.EFFECT_STATUS_CHANGED,
            occurred_at=now,
            producer_service="effect-executor",
            payload_schema="effect/v1",
            payload=EffectStatusChangedPayload(
                kind="effect.status.changed",
                effect_id=effect.effect_id,
                status=status,
            ),
            attempt_id=None,
            causation_event_id=None,
            trace_id=token,
        )
        await tx.append_event(event, run.last_event_seq)
        await tx.replace_run_cas(
            replace(
                run,
                version=run.version + 1,
                last_event_seq=event.event_seq,
                updated_at=now,
            ),
            run.version,
        )


@runtime_checkable
class EffectCapabilityAuthorizer(Protocol):
    """Verify the tenant/effect-bound capability that guards one Effect."""

    async def verify_effect(
        self,
        token: str,
        *,
        tenant_id: str,
        effect_id: str,
        approval_id: str,
        request_digest: str,
        required_scopes: tuple[str, ...],
    ) -> VerifiedEffectCapability: ...


@runtime_checkable
class EffectPayloadResolver(Protocol):
    """Resolve the canonical Effect payload from its restricted reference."""

    async def resolve(
        self, *, tenant_id: str, payload_ref: str
    ) -> ResolvedEffectPayload: ...


@runtime_checkable
class EffectReconciliationAuthorizer(Protocol):
    """Authorize a reconciliation observation against an Effect's evidence."""

    async def verify_reconciliation(
        self,
        token: str,
        *,
        tenant_id: str,
        effect_id: str,
        effect_key: str,
        evidence_digest: str,
    ) -> VerifiedEffectReconciliation: ...
