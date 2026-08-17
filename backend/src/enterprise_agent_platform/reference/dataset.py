"""Small deterministic dataset for the portable reference workflow.

The records are intentionally synthetic and organization-neutral. They model only
test outcomes and failure signals, never a host application's business objects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CaseOutcome = Literal["PASSED", "FAILED"]


@dataclass(frozen=True, slots=True)
class SyntheticCase:
    case_id: str
    suite: str
    outcome: CaseOutcome
    duration_ms: int
    signal_code: str | None
    source_ref: str

    def __post_init__(self) -> None:
        if not self.case_id or not self.suite or self.duration_ms <= 0:
            raise ValueError("synthetic case is invalid")
        if self.source_ref != f"synthetic-case:{self.case_id}":
            raise ValueError("synthetic case source ref is invalid")
        if (self.outcome == "FAILED") != (self.signal_code is not None):
            raise ValueError("only failed cases carry a failure signal")


REFERENCE_DATASET_VERSION = "reference/v1"
REFERENCE_RESOURCE_REF = f"synthetic-dataset:{REFERENCE_DATASET_VERSION}"

SYNTHETIC_CASES: tuple[SyntheticCase, ...] = (
    SyntheticCase(
        case_id="case-001",
        suite="checkout",
        outcome="PASSED",
        duration_ms=82,
        signal_code=None,
        source_ref="synthetic-case:case-001",
    ),
    SyntheticCase(
        case_id="case-002",
        suite="checkout",
        outcome="FAILED",
        duration_ms=91,
        signal_code="ASSERTION_MISMATCH",
        source_ref="synthetic-case:case-002",
    ),
    SyntheticCase(
        case_id="case-003",
        suite="reporting",
        outcome="PASSED",
        duration_ms=47,
        signal_code=None,
        source_ref="synthetic-case:case-003",
    ),
    SyntheticCase(
        case_id="case-004",
        suite="reporting",
        outcome="FAILED",
        duration_ms=1_500,
        signal_code="TIMEOUT",
        source_ref="synthetic-case:case-004",
    ),
    SyntheticCase(
        case_id="case-005",
        suite="analytics",
        outcome="FAILED",
        duration_ms=730,
        signal_code="REGRESSION_THRESHOLD",
        source_ref="synthetic-case:case-005",
    ),
    SyntheticCase(
        case_id="case-006",
        suite="analytics",
        outcome="PASSED",
        duration_ms=420,
        signal_code=None,
        source_ref="synthetic-case:case-006",
    ),
)

__all__ = [
    "REFERENCE_DATASET_VERSION",
    "REFERENCE_RESOURCE_REF",
    "SYNTHETIC_CASES",
    "CaseOutcome",
    "SyntheticCase",
]
