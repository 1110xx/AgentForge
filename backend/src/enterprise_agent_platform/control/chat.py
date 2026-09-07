"""Free-form chat intent parsing (Phase 3.6 frontend entry).

MVP: keyword-table mapping that covers the demo workflow; anything unmatched
falls back to DEFAULT_WORKFLOW with the raw message as the Run intent. The
classifier is a pure function with a single entry point, so a real LLM-based
resolver can be swapped in later without touching the route or contracts.

``it-incident-investigate`` (B-档 incident vertical) is keyword-selected by
ticket vocabulary (工单 / ticket / incident / outage). When that workflow is
chosen and the client still carries the built-in synthetic default resource
ref, ``resolve_chat_resources`` derives the ``incident://ticket/<id>`` ref
from the message so the created Run points at the real business data.
"""

from __future__ import annotations

import re

from enterprise_agent_platform.contracts.models import StrictModel

DEFAULT_WORKFLOW = "synthetic-analysis"
INCIDENT_WORKFLOW = "it-incident-investigate"

# Keyword table: workflow_type -> trigger keywords (case-insensitive).
# Only registered workflows belong here (WORKFLOW_PARAMETER_MODELS).
# Incident keywords intentionally sit first so a ticket-style message wins over
# the generic synthetic keywords (分析 / 日志 / 故障 / ...).
_WORKFLOW_KEYWORDS: dict[str, tuple[str, ...]] = {
    INCIDENT_WORKFLOW: (
        "工单",
        "ticket",
        "incident",
        "事故",
        "outage",
        "sev",
    ),
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

_INCIDENT_TICKET_IN_TEXT = re.compile(
    r"incident://ticket/(?P<uri>[Tt]\d{6,})"
    r"|(?:工单|ticket|tkt)\s*[#:：]?\s*(?P<plain>[Tt]\d{6,})",
    re.IGNORECASE,
)


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


def extract_incident_ticket_id(message: str) -> str | None:
    """Find the ticket id referenced in a natural-language message."""
    match = _INCIDENT_TICKET_IN_TEXT.search(message)
    if match is None:
        return None
    ticket_id = match.group("uri") or match.group("plain")
    return ticket_id.upper()


def resolve_chat_resources(
    plan: IntentPlan, message: str, resource_refs: tuple[str, ...]
) -> tuple[str, ...]:
    """Derive the business resource ref for incident chat messages.

    For the incident workflow, the built-in synthetic default refs are replaced
    by the ticket the user referenced (falling back to the deterministic demo
    ticket). Non-incident workflows keep the client-provided refs untouched.
    """
    if plan.workflow_type != INCIDENT_WORKFLOW:
        return resource_refs
    if not all(ref.startswith("synthetic-") for ref in resource_refs):
        return resource_refs
    ticket_id = extract_incident_ticket_id(message)
    if ticket_id is None:
        from enterprise_agent_platform.reference.incident import (
            INCIDENT_DEMO_TICKET_ID,
        )

        ticket_id = INCIDENT_DEMO_TICKET_ID
    return (f"incident://ticket/{ticket_id}",)


__all__ = [
    "DEFAULT_WORKFLOW",
    "INCIDENT_WORKFLOW",
    "IntentPlan",
    "classify_intent",
    "extract_incident_ticket_id",
    "resolve_chat_resources",
]
