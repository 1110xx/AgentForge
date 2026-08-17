"""Verified runtime and effect capabilities (reconstructed stub)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class VerifiedRuntimeCapability:
    token_id: str
    issuer: str
    audience: str
    tenant_id: str
    run_id: str
    execution_unit_id: str
    attempt_id: str
    generation: int
    scopes: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedEffectCapability:
    token_id: str
    issuer: str
    audience: str
    tenant_id: str
    effect_id: str
    approval_id: str
    request_digest: str
    tool_name: str
    tool_version: str
    tool_spec_digest: str
    connector_name: str
    canonical_target: str
    scopes: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime


@runtime_checkable
class CapabilityIssuer(Protocol):
    """Issue short-lived Runtime and Effect capability tokens."""

    async def issue_runtime(
        self,
        *,
        tenant_id: str,
        run_id: str,
        execution_unit_id: str,
        attempt_id: str,
        generation: int,
        scopes: tuple[str, ...],
        ttl_seconds: int,
    ) -> str: ...

    async def issue_effect(
        self,
        *,
        tenant_id: str,
        effect_id: str,
        approval_id: str,
        request_digest: str,
        tool_name: str,
        tool_version: str,
        tool_spec_digest: str,
        connector_name: str,
        canonical_target: str,
        scopes: tuple[str, ...],
        ttl_seconds: int,
    ) -> str: ...


@runtime_checkable
class CapabilityVerifier(Protocol):
    """Verify and decode Runtime and Effect capability tokens."""

    async def verify_runtime(
        self,
        token: str,
        *,
        tenant_id: str,
        run_id: str,
        attempt_id: str,
        generation: int,
        required_scopes: tuple[str, ...],
    ) -> VerifiedRuntimeCapability: ...

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
