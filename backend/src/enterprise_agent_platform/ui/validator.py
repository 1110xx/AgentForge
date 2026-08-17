"""Surface document validation against the fixed A2UI catalog."""
# ruff: noqa: TRY004 - ValueError is the validator's contract-violation signal
from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue

from enterprise_agent_platform.ui.catalog import A2UI_PROTOCOL_VERSION, PUBLIC_CATALOG_ID

_ALLOWED_COMPONENTS = frozenset(
    {
        "ProgressCard",
        "EvidenceSummary",
        "ArtifactCard",
        "ApprovalCard",
        "StaleCard",
    }
)


@dataclass(frozen=True, slots=True)
class SurfaceValidator:
    catalog_id: str
    protocol_version: str

    def validate(self, document: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if not isinstance(document, dict):
            raise ValueError("surface document must be an object")
        if self.catalog_id != PUBLIC_CATALOG_ID:
            raise ValueError("catalog is not enabled on this surface service")
        if self.protocol_version != A2UI_PROTOCOL_VERSION:
            raise ValueError("A2UI protocol version is not enabled on this surface service")
        component = document.get("component")
        if not isinstance(component, str) or component not in _ALLOWED_COMPONENTS:
            raise ValueError("surface component is not in the allowlisted catalog")
        props = document.get("props")
        if not isinstance(props, dict):
            raise ValueError("surface props must be an object")
        return {
            "valid": True,
            "catalog_id": self.catalog_id,
            "protocol_version": self.protocol_version,
            "component": component,
        }
