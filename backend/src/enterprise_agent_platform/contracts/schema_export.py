"""Deterministic JSON Schema exporter used by checked-in golden contracts."""
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from .commands import CreateRunCommand, UiActionCommand
from .errors import ApiErrorEnvelope
from .events import EnterpriseEventEnvelope, EventPayload
from .models import (
    Approval,
    ArtifactDownloadAuthorization,
    EffectCapabilityClaims,
    RunEventPage,
    RuntimeCapabilityClaims,
    RunViewSnapshot,
    SurfaceRevision,
    ToolInvocation,
)

SCHEMAS: dict[str, Any] = {
    "enterprise-event.schema.json": EnterpriseEventEnvelope,
    "create-run-command.schema.json": CreateRunCommand,
    "ui-action.schema.json": UiActionCommand,
    "tool-invocation.schema.json": ToolInvocation,
    "approval.schema.json": Approval,
    "capability-claims.schema.json": (RuntimeCapabilityClaims, EffectCapabilityClaims),
    "api-error.schema.json": ApiErrorEnvelope,
    "run-view-snapshot.schema.json": RunViewSnapshot,
    "run-event-page.schema.json": RunEventPage,
    "artifact-download-authorization.schema.json": ArtifactDownloadAuthorization,
    "a2ui-surface-revision.schema.json": SurfaceRevision,
    "event-payload-union.schema.json": EventPayload,
}


def export_contracts(output_dir: Path) -> dict[str, str]:
    """Write canonical schemas and return their content-addressed digests."""
    output_dir.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    for filename, model in SCHEMAS.items():
        schema = _schema_for(model)
        content = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (output_dir / filename).write_text(content)
        digests[filename] = hashlib.sha256(content.encode()).hexdigest()
    return digests


def _schema_for(model: Any) -> dict[str, Any]:
    if isinstance(model, type) and issubclass(model, BaseModel):
        return model.model_json_schema()
    if isinstance(model, tuple):
        return {"oneOf": [item.model_json_schema() for item in model]}
    return TypeAdapter(model).json_schema()
