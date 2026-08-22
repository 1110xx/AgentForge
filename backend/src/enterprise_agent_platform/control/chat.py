"""Free-form chat intent parsing (Phase 3.6 frontend entry).

MVP: keyword-table mapping that covers the demo workflow; anything unmatched
falls back to DEFAULT_WORKFLOW with the raw message as the Run intent. The
classifier is a pure function with a single entry point, so a real LLM-based
resolver can be swapped in later without touching the route or contracts.
"""
from __future__ import annotations

from enterprise_agent_platform.contracts.models import StrictModel

DEFAULT_WORKFLOW = "synthetic-analysis"

# Keyword table: workflow_type -> trigger keywords (case-insensitive).
# Only registered workflows belong here (WORKFLOW_PARAMETER_MODELS).
_WORKFLOW_KEYWORDS: dict[str, tuple[str, ...]] = {
    "synthetic-analysis": (
        "分析",
        "日志",
        "故障",
        "失败",
        "analyze",
        "analyse",
        "failure",
        "error",
        "log",
        "pattern",
    ),
}


class IntentPlan(StrictModel):
    schema_version: str = "intent-plan/v1"
    workflow_type: str
    intent: str


def classify_intent(message: str, workflow_hint: str | None = None) -> IntentPlan:
    """Map a natural-language message to a workflow type + Run intent.

    ``workflow_hint`` (non-empty) wins verbatim; otherwise the first workflow
    whose keyword hits the message wins; unmatched messages fall back to
    ``DEFAULT_WORKFLOW``. The intent always keeps the raw message (trimmed).
    """
    text = message.strip()
    if workflow_hint:
        return IntentPlan(workflow_type=workflow_hint, intent=text)
    lowered = text.lower()
    for workflow, keywords in _WORKFLOW_KEYWORDS.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            return IntentPlan(workflow_type=workflow, intent=text)
    return IntentPlan(workflow_type=DEFAULT_WORKFLOW, intent=text)


__all__ = [
    "DEFAULT_WORKFLOW",
    "IntentPlan",
    "classify_intent",
]