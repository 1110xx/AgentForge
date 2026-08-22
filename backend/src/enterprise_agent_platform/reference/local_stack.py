"""Explicit API-only local reference composition for copy-and-run evaluation.

Nothing imports this module implicitly. Production entrypoints still require host
factories, durable persistence, a scheduler and a worker-implementation.
"""
from __future__ import annotations

import hashlib

from fastapi import FastAPI

from enterprise_agent_platform import (
    AgentPlatformContainer,
    HostPortError,
    RequestContext,
    ResolvedPolicyContext,
    ResolvedResource,
    VerifiedHostContext,
    create_in_memory_container,
)
from enterprise_agent_platform import create_app as create_platform_app
from enterprise_agent_platform.reference.session import InMemoryRunSessionProvider

REFERENCE_LOCAL_BEARER = "Bearer reference-local-demo"
REFERENCE_LOCAL_TENANT = "reference-local"


class ReferenceLocalAuth:
    async def authenticate(
        self,
        authorization: str | None,
        request_id: str,
        trace_id: str | None,
    ) -> RequestContext:
        if authorization != REFERENCE_LOCAL_BEARER:
            raise HostPortError("UNAUTHENTICATED", "reference local token is required")
        return RequestContext(
            tenant_id=REFERENCE_LOCAL_TENANT,
            actor_id="reference-local-analyst",
            scopes=("runs:create", "runs:read", "runs:cancel"),
            request_id=request_id,
            trace_id=trace_id,
        )


class ReferenceSyntheticResources:
    async def resolve(self, ctx: RequestContext, resource_ref: str) -> ResolvedResource:
        if not resource_ref.startswith(("synthetic-case:", "synthetic-dataset:")):
            raise HostPortError("NOT_FOUND", "reference resource was not found")
        digest = hashlib.sha256(resource_ref.encode()).hexdigest()
        return ResolvedResource(
            resource_ref=resource_ref,
            canonical_id=resource_ref.partition(":")[0],
            tenant_id=ctx.tenant_id,
            owner_id="reference-local-team",
            classification="synthetic",
            version="reference-resource/v1",
            digest=f"sha256:{digest}",
        )


class ReferenceHostContextVerifier:
    async def verify(self, ctx: RequestContext, host_context_ref: str) -> VerifiedHostContext:
        if not host_context_ref.startswith("reference-context:"):
            raise HostPortError("HOST_CONTEXT_FORGED", "reference context is invalid")
        digest = hashlib.sha256(host_context_ref.encode()).hexdigest()
        return VerifiedHostContext(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            digest=f"sha256:{digest}",
            version="reference-context/v1",
        )


class ReferenceAllowAllPolicy:
    async def resolve(
        self,
        ctx: RequestContext,
        workflow_type: str,
        resources: tuple[ResolvedResource, ...],
        host_context: VerifiedHostContext | None,
    ) -> ResolvedPolicyContext:
        del ctx, host_context
        return ResolvedPolicyContext(
            allowed=True,
            policy_version="reference-policy/v1",
            policy_digest="sha256:reference-local-policy",
            scopes=("synthetic:read",),
            budget={"max_tool_calls": 10, "max_runtime_seconds": 60},
            denial_code=None,
        )


def create_container() -> AgentPlatformContainer:
    """Create a fresh process-local, non-durable API container."""
    return create_in_memory_container(
        auth_context_provider=ReferenceLocalAuth(),
        resource_resolver=ReferenceSyntheticResources(),
        host_context_verifier=ReferenceHostContextVerifier(),
        policy_context_provider=ReferenceAllowAllPolicy(),
        run_sessions=InMemoryRunSessionProvider(),
    )


def create_app() -> FastAPI:
    return create_platform_app(create_container())


__all__ = [
    "REFERENCE_LOCAL_BEARER",
    "REFERENCE_LOCAL_TENANT",
    "ReferenceAllowAllPolicy",
    "ReferenceHostContextVerifier",
    "ReferenceLocalAuth",
    "ReferenceSyntheticResources",
    "create_app",
    "create_container",
]
