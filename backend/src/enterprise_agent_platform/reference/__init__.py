"""Portable synthetic reference adapter and executable in-process vertical."""
from enterprise_agent_platform.reference.adapter import (
    SyntheticAnalysis,
    SyntheticAnalysisAdapter,
    SyntheticReadResult,
)
from enterprise_agent_platform.reference.dataset import (
    REFERENCE_RESOURCE_REF,
    SYNTHETIC_CASES,
)
from enterprise_agent_platform.reference.provider import ReferenceWorkflowHarness

__all__ = [
    "REFERENCE_RESOURCE_REF",
    "SYNTHETIC_CASES",
    "ReferenceWorkflowHarness",
    "SyntheticAnalysis",
    "SyntheticAnalysisAdapter",
    "SyntheticReadResult",
]
