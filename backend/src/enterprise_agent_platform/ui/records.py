"""Published UI surface records (reconstructed stub)."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class PublishedSurfaceRevision:
    revision: int


@dataclass(frozen=True, slots=True)
class PublishedSurface:
    surface_id: str
    revision: PublishedSurfaceRevision
    document: dict[str, JsonValue]
