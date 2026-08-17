"""Idempotent fake external defect connector for process-local verification only."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from enterprise_agent_platform.tools.connectors import (
    ConnectorCallContext,
    ConnectorKnownFailure,
    ConnectorOutcomeUnknown,
    CredentialMaterial,
)

FailureMode = Literal["none", "known_failure", "unknown_after_commit"]


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class FakeDefectRecord:
    defect_id: str
    effect_key: str
    target: str
    payload_digest: str


class FakeDefectConnector:
    """The shared DurableEffectExecutor supplies its durable effect_key through
    ConnectorCallContext.idempotency_key. No credential is retained in records or
    output."""

    def __init__(self, *, failure_mode: FailureMode = "none") -> None:
        self._failure_mode = failure_mode
        self._records: dict[str, FakeDefectRecord] = {}
        self.call_count = 0
        self.create_count = 0

    @property
    def records(self) -> tuple[FakeDefectRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    async def invoke(
        self,
        context: ConnectorCallContext,
        operation: str,
        resource_ref: str,
        arguments: dict[str, object],
        credential: CredentialMaterial,
    ) -> dict[str, object]:
        self.call_count += 1
        if (
            operation != "defect.create"
            or resource_ref != "project:reference"
            or credential.secret.get("reference_key") != "reference-only"
        ):
            raise ConnectorKnownFailure("invalid reference connector invocation")
        effect_key = context.idempotency_key
        if effect_key is None or not effect_key.startswith("sha256:"):
            raise ConnectorKnownFailure("durable Effect idempotency key is missing")
        payload_digest = _digest(arguments)
        existing = self._records.get(effect_key)
        if existing is not None and (
            existing.payload_digest != payload_digest or existing.target != resource_ref
        ):
            raise ConnectorKnownFailure("effect key payload conflict")
        if self._failure_mode == "known_failure":
            raise ConnectorKnownFailure("injected known failure")
        defect_id = f"DEF-{hashlib.sha256(effect_key.encode()).hexdigest()[:10].upper()}"
        record = FakeDefectRecord(
            defect_id=defect_id,
            effect_key=effect_key,
            target=resource_ref,
            payload_digest=payload_digest,
        )
        self._records[effect_key] = record
        self.create_count += 1
        if self._failure_mode == "unknown_after_commit":
            raise ConnectorOutcomeUnknown("injected unknown outcome after commit")
        return {"remote_id": record.defect_id, "status": "created"}


__all__ = ["FailureMode", "FakeDefectConnector", "FakeDefectRecord"]
