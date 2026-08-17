"""Tool registry and grants (reconstructed stub)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    version: str
    connector_name: str
    operation: str
    risk_class: str
    required_scopes: tuple[str, ...]
    allowed_resource_prefixes: tuple[str, ...]
    required_input_fields: tuple[str, ...]
    optional_input_fields: tuple[str, ...]
    required_output_fields: tuple[str, ...]
    timeout_seconds: float
    max_result_bytes: int


@dataclass(frozen=True, slots=True)
class ToolGrant:
    grant_id: str
    tenant_id: str
    run_id: str
    attempt_id: str
    tool_name: str
    tool_version: str
    scopes: tuple[str, ...]
    resource_prefixes: tuple[str, ...]
    active: bool


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[tuple[str, str], ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._specs[(spec.name, spec.version)] = spec

    def get(self, name: str, version: str) -> ToolSpec | None:
        return self._specs.get((name, version))


class InMemoryToolGrantStore:
    def __init__(self) -> None:
        self._grants: dict[str, ToolGrant] = {}

    def add(self, grant: ToolGrant) -> None:
        self._grants[grant.grant_id] = grant

    def get(self, grant_id: str) -> ToolGrant | None:
        return self._grants.get(grant_id)
