"""Trusted host ports without imports from an embedding business application."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from enterprise_agent_platform.contracts.commands import CreateRunCommand
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.domain.records import RunAuthorizationContext

OPAQUE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,511}$")
SAFE_TEXT = re.compile(r"[^\x00-\x1f\x7f]{1,512}$")
FORBIDDEN_POLICY_KEY = re.compile(
    r"(?:credential|password|secret|token|bearer|endpoint|callback|uri|url)", re.IGNORECASE
)


class HostPortError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ResolvedResource:
    resource_ref: str
    canonical_id: str
    tenant_id: str
    owner_id: str
    classification: str
    version: str
    digest: str


@dataclass(frozen=True, slots=True)
class VerifiedHostContext:
    tenant_id: str
    actor_id: str
    digest: str
    version: str


@dataclass(frozen=True, slots=True)
class ResolvedPolicyContext:
    allowed: bool
    policy_version: str
    policy_digest: str
    scopes: tuple[str, ...]
    budget: dict[str, JsonValue]
    denial_code: str | None = None


@runtime_checkable
class AuthContextProvider(Protocol):
    async def authenticate(
        self,
        authorization: str | None,
        request_id: str,
        trace_id: str | None,
    ) -> RequestContext: ...


@runtime_checkable
class ResourceResolver(Protocol):
    async def resolve(self, ctx: RequestContext, resource_ref: str) -> ResolvedResource: ...


@runtime_checkable
class HostContextVerifier(Protocol):
    async def verify(self, ctx: RequestContext, host_context_ref: str) -> VerifiedHostContext: ...


@runtime_checkable
class PolicyContextProvider(Protocol):
    async def resolve(
        self,
        ctx: RequestContext,
        workflow_type: str,
        resources: tuple[ResolvedResource, ...],
        host_context: VerifiedHostContext | None,
    ) -> ResolvedPolicyContext: ...


def validate_opaque_reference(value: str) -> None:
    """Ensure an opaque ID cannot choose a host, scheme, path or header."""
    if (
        not OPAQUE_REF.fullmatch(value)
        or "://" in value
        or value.startswith(("/", "\\"))
        or "@" in value
    ):
        raise HostPortError("INVALID_OPAQUE_REFERENCE", "opaque reference is invalid")


def validate_safe_text(value: str, field: str) -> None:
    if not SAFE_TEXT.fullmatch(value) or "://" in value:
        raise HostPortError("HOST_RESPONSE_INVALID", f"host {field} is invalid")


def _canonical_json_object(value: JsonValue) -> dict[str, JsonValue]:
    try:
        canonical = json.loads(
            json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
        )
    except (TypeError, ValueError) as error:
        raise HostPortError("HOST_RESPONSE_INVALID", "host policy budget is invalid") from error
    if not isinstance(canonical, dict):
        raise HostPortError("HOST_RESPONSE_INVALID", "host policy budget must be an object")
    return canonical


def validate_policy_budget(value: JsonValue) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if FORBIDDEN_POLICY_KEY.search(key):
                raise HostPortError(
                    "HOST_RESPONSE_INVALID", "host policy budget contains forbidden authority data"
                )
            validate_policy_budget(nested)
    elif isinstance(value, list):
        for item in value:
            validate_policy_budget(item)
    elif isinstance(value, str):
        validate_safe_text(value, "policy budget value")


async def _call_port[T](awaitable: object, timeout_seconds: float) -> T:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except HostPortError:
        raise
    except (TimeoutError, ConnectionError, OSError) as error:
        raise HostPortError(
            "HOST_PORT_UNAVAILABLE",
            "host integration is temporarily unavailable",
            retryable=True,
        ) from error


async def resolve_run_authorization(
    ctx: RequestContext,
    command: CreateRunCommand,
    *,
    resource_resolver: ResourceResolver,
    host_context_verifier: HostContextVerifier,
    policy_context_provider: PolicyContextProvider,
    timeout_seconds: float,
) -> RunAuthorizationContext:
    """Resolve mutable host authority into one canonical immutable snapshot input."""
    if timeout_seconds <= 0:
        raise ValueError("host port timeout must be positive")

    resources: list[ResolvedResource] = []
    for resource_ref in command.resource_refs:
        validate_opaque_reference(resource_ref)
        resolved = await _call_port(
            resource_resolver.resolve(ctx, resource_ref), timeout_seconds
        )
        if resolved.tenant_id != ctx.tenant_id:
            raise HostPortError("NOT_FOUND", "resource was not found")
        if resolved.resource_ref != resource_ref:
            raise HostPortError("HOST_RESPONSE_INVALID", "host resource reference changed")
        for field, value in (
            ("canonical identity", resolved.canonical_id),
            ("owner", resolved.owner_id),
            ("classification", resolved.classification),
            ("version", resolved.version),
            ("digest", resolved.digest),
        ):
            validate_safe_text(value, field)
        resources.append(resolved)

    verified_context: VerifiedHostContext | None = None
    if command.host_context_ref is not None:
        validate_opaque_reference(command.host_context_ref)
        verified_context = await _call_port(
            host_context_verifier.verify(ctx, command.host_context_ref),
            timeout_seconds,
        )
        if verified_context.tenant_id != ctx.tenant_id or verified_context.actor_id != ctx.actor_id:
            raise HostPortError("FORBIDDEN", "host context binding is invalid")
        validate_safe_text(verified_context.digest, "context digest")
        validate_safe_text(verified_context.version, "context version")

    policy = await _call_port(
        policy_context_provider.resolve(
            ctx,
            command.workflow_type,
            tuple(resources),
            verified_context,
        ),
        timeout_seconds,
    )
    if not policy.allowed:
        raise HostPortError(policy.denial_code or "POLICY_DENIED", "host policy denied the Run")
    validate_safe_text(policy.policy_version, "policy version")
    for scope in policy.scopes:
        validate_safe_text(scope, "policy scope")
    budget = _canonical_json_object(policy.budget)
    validate_policy_budget(budget)

    resolved_resources: tuple[dict[str, JsonValue], ...] = tuple(
        {
            "canonical_id": item.canonical_id,
            "classification": item.classification,
            "digest": item.digest,
            "owner_id": item.owner_id,
            "resource_ref": item.resource_ref,
            "version": item.version,
        }
        for item in resources
    )
    digest_payload = {
        "host_context_digest": (
            verified_context.digest if verified_context is not None else None
        ),
        "host_context_version": (
            verified_context.version if verified_context is not None else None
        ),
        "policy_budget": budget,
        "policy_digest": policy.policy_digest,
        "policy_scopes": list(policy.scopes),
        "policy_version": policy.policy_version,
        "resolved_resources": list(resolved_resources),
    }
    canonical = json.dumps(digest_payload, separators=(",", ":"), sort_keys=True)
    return RunAuthorizationContext(
        resolved_resources=resolved_resources,
        host_context_digest=(verified_context.digest if verified_context is not None else None),
        host_context_version=(verified_context.version if verified_context is not None else None),
        policy_digest=policy.policy_digest,
        policy_version=policy.policy_version,
        policy_scopes=tuple(policy.scopes),
        policy_budget=budget,
        snapshot_digest=f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
    )
