"""Connector call ports.

A Connector is the only place that touches an external system. It receives an
idempotency/effect key in the call context, the operation and resource from the
frozen ToolSpec/Effect snapshot, validated arguments and a short-lived
CredentialMaterial. A Connector must raise ConnectorKnownFailure for a definite
rejection and ConnectorOutcomeUnknown when it cannot tell whether the external
operation committed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class ConnectorCallContext:
    idempotency_key: str | None = None
    effect_key: str | None = None


@dataclass(frozen=True, slots=True)
class CredentialMaterial:
    secret: dict[str, str] = field(default_factory=dict)


class ConnectorKnownFailure(RuntimeError):
    pass


class ConnectorOutcomeUnknown(RuntimeError):
    pass


@runtime_checkable
class Connector(Protocol):
    """External boundary: one idempotent operation invocation."""

    async def invoke(
        self,
        context: ConnectorCallContext,
        operation: str,
        resource_ref: str,
        arguments: dict[str, object],
        credential: CredentialMaterial,
    ) -> dict[str, JsonValue]: ...


@runtime_checkable
class CredentialBroker(Protocol):
    """Issue short-lived Connector credentials for a tenant and resource."""

    async def acquire(
        self,
        *,
        tenant_id: str,
        connector_name: str,
        resource_ref: str,
    ) -> CredentialMaterial: ...
