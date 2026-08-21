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
from enterprise_agent_platform.reference.deepseek_provider import DeepSeekModelSessionProvider, create_deepseek_provider
from enterprise_agent_platform.reference.model_provider import ReferenceModelSessionProvider
from enterprise_agent_platform.reference.provider import ReferenceWorkflowHarness
from enterprise_agent_platform.reference.session import InMemoryRunSessionProvider

__all__ = [
    "REFERENCE_RESOURCE_REF",
    "SYNTHETIC_CASES",
    "InMemoryRunSessionProvider",
    "ReferenceModelSessionProvider",
    "DeepSeekModelSessionProvider",
    "create_deepseek_provider",
    "ReferenceWorkflowHarness",
    "SyntheticAnalysis",
    "SyntheticAnalysisAdapter",
    "SyntheticReadResult",
]
