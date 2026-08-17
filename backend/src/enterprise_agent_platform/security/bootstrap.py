"""Runtime bootstrap claim response (reconstructed stub)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BootstrapResponse:
    runtime_token: str
    tenant_id: str
    run_id: str
    execution_unit_id: str
    attempt_id: str
    generation: int
