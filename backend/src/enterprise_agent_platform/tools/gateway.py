"""Tool gateway ports and the reference-gateway implementation.

The gateway is the only public boundary through which Runtime tool calls reach
Connectors. It re-checks the frozen ToolSpec, the grant, the runtime capability
and the input/output contracts before invoking the Connector, then records the
immutable result in the result store under a content-addressed reference.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import JsonValue

from enterprise_agent_platform.persistence.protocol import PlatformError
from enterprise_agent_platform.tools.connectors import ConnectorCallContext


@dataclass(frozen=True, slots=True)
class GatewayAuthorization:
    principal_scopes: tuple[str, ...]
    run_policy_scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolInvocationRequest:
    tenant_id: str
    run_id: str
    execution_unit_id: str
    attempt_id: str
    generation: int
    call_id: str
    tool_name: str
    tool_version: str
    grant_id: str
    resource_ref: str
    arguments: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ToolInvocationResult:
    result_ref: str


class InMemoryInvocationRepository:
    def __init__(self) -> None:
        self._invocations: dict[str, ToolInvocationResult] = {}

    async def put(self, result: ToolInvocationResult) -> None:
        self._invocations[result.result_ref] = result


class ToolGateway:
    """Re-verify and execute one Runtime tool call through a Connector.

    The gateway is deliberately strict: a missing grant, an inactive grant, a
    scope or resource-prefix mismatch, a malformed argument set, a connector
    contract violation or an oversized payload all fail closed with a stable
    PlatformError code.
    """

    def __init__(
        self,
        *,
        registry: Any,
        grants: Any,
        capability_verifier: Any,
        credential_broker: Any,
        connectors: dict[str, Any],
        invocations: InMemoryInvocationRepository,
        results: Any,
        authorization_provider: Any,
    ) -> None:
        self.registry = registry
        self.grants = grants
        self.capability_verifier = capability_verifier
        self.credential_broker = credential_broker
        self.connectors = connectors
        self.invocations = invocations
        self.results = results
        self.authorization_provider = authorization_provider

    async def invoke(self, token: str, request: ToolInvocationRequest) -> ToolInvocationResult:
        authorization = self.authorization_provider()
        spec = self.registry.get(request.tool_name, request.tool_version)
        if spec is None:
            raise PlatformError(
                "TOOL_NOT_FOUND", f"tool {request.tool_name} {request.tool_version} is not registered"
            )
        if not set(spec.required_scopes) <= set(authorization.run_policy_scopes):
            raise PlatformError(
                "SCOPE_DENIED", "run policy does not grant the tool's required scopes"
            )
        grant = self.grants.get(request.grant_id)
        if grant is None:
            raise PlatformError("GRANT_NOT_FOUND", "tool grant does not exist")
        if not grant.active:
            raise PlatformError("GRANT_INACTIVE", "tool grant is not active")
        if grant.tool_name != request.tool_name or grant.tool_version != request.tool_version:
            raise PlatformError("GRANT_MISMATCH", "grant does not match the requested tool")
        missing_scopes = set(spec.required_scopes) - set(grant.scopes)
        if missing_scopes:
            raise PlatformError(
                "SCOPE_DENIED", f"grant lacks required scopes: {sorted(missing_scopes)}"
            )
        if not any(
            request.resource_ref.startswith(prefix) for prefix in spec.allowed_resource_prefixes
        ):
            raise PlatformError(
                "RESOURCE_DENIED", "resource is outside the grant's allowed prefixes"
            )

        capability = await self.capability_verifier.verify_runtime(
            token,
            tenant_id=request.tenant_id,
            run_id=request.run_id,
            attempt_id=request.attempt_id,
            generation=request.generation,
            required_scopes=spec.required_scopes,
        )
        if (
            capability.run_id != request.run_id
            or capability.attempt_id != request.attempt_id
            or capability.generation != request.generation
            or capability.expires_at <= datetime.now(UTC)
        ):
            raise PlatformError(
                "CAPABILITY_MISMATCH", "runtime capability does not match the invocation"
            )

        credential = await self.credential_broker.acquire(
            tenant_id=request.tenant_id,
            connector_name=spec.connector_name,
            resource_ref=request.resource_ref,
        )

        allowed_inputs = set(spec.required_input_fields) | set(spec.optional_input_fields)
        missing_inputs = set(spec.required_input_fields) - set(request.arguments)
        if missing_inputs:
            raise PlatformError(
                "INPUT_MISSING", f"tool requires input fields: {sorted(missing_inputs)}"
            )
        unexpected = set(request.arguments) - allowed_inputs
        if unexpected:
            raise PlatformError(
                "INPUT_UNEXPECTED", f"tool does not accept input fields: {sorted(unexpected)}"
            )

        connector = self.connectors.get(spec.connector_name)
        if connector is None:
            raise PlatformError(
                "CONNECTOR_NOT_FOUND", f"connector {spec.connector_name} is not configured"
            )
        try:
            output = await asyncio.wait_for(
                connector.invoke(
                    ConnectorCallContext(idempotency_key=request.call_id),
                    operation=spec.operation,
                    resource_ref=request.resource_ref,
                    arguments=request.arguments,
                    credential=credential,
                ),
                timeout=spec.timeout_seconds,
            )
        except TimeoutError as error:
            raise PlatformError("CONNECTOR_TIMEOUT", "connector call exceeded its deadline") from error

        if not isinstance(output, dict):
            raise PlatformError("OUTPUT_INVALID", "connector output must be an object")
        missing_outputs = set(spec.required_output_fields) - set(output)
        if missing_outputs:
            raise PlatformError(
                "OUTPUT_MISSING", f"connector output lacks required fields: {sorted(missing_outputs)}"
            )

        payload = json.dumps(output, sort_keys=True, separators=(",", ":")).encode()
        if len(payload) > spec.max_result_bytes:
            raise PlatformError("RESULT_TOO_LARGE", "tool result exceeds max_result_bytes")
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        result_ref = await self.results.put(digest, payload)

        result = ToolInvocationResult(result_ref=result_ref)
        await self.invocations.put(result)
        return result
