"""Hermetic tests for the it-incident-investigate business vertical (B-档).

Covers: dataset integrity, incident:// URI parsing, canonical payload
rendering (SDD §3.2.4 schemas), ResourceResolver behaviour (SDD §3.2.5 error
contracts + synthetic fallback), workflow parameter-model registration, and
the chat intent mapping used by the front-end launcher.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from enterprise_agent_platform.contracts.commands import (
    WORKFLOW_PARAMETER_MODELS,
    CreateRunCommand,
)
from enterprise_agent_platform.control.chat import (
    classify_intent,
    extract_incident_ticket_id,
    resolve_chat_resources,
)
from enterprise_agent_platform.integration.host import HostPortError
from enterprise_agent_platform.reference.incident import (
    INCIDENT_DEMO_TICKET_ID,
    IncidentResources,
    get_incident_ticket,
    incident_payload,
    incident_tickets,
    logs_payload,
    metrics_payload,
    parse_incident_uri,
    service_window,
)


class _Ctx:
    def __init__(self, tenant_id: str = "tenant-a") -> None:
        self.tenant_id = tenant_id


def _run(awaitable):
    return asyncio.run(awaitable)


# ---------------------------------------------------------------------------
# Dataset integrity (SDD §4.4)
# ---------------------------------------------------------------------------


def test_dataset_registers_two_scenarios():
    tickets = incident_tickets()
    ids = {ticket.ticket_id for ticket in tickets}
    assert ids == {"T20260907", "T20260908"}
    rich = get_incident_ticket("T20260907")
    assert rich is not None
    assert rich.severity == "P2"
    assert rich.affected_services == ("pay-service",)
    assert rich.title == "支付服务大量接口504超时"
    sparse = get_incident_ticket("T20260908")
    assert sparse is not None
    assert sparse.severity == "P3"
    assert sparse.affected_services == ("order-service",)


def test_pay_service_logs_cover_failure_timeline():
    start, end = service_window("pay-service")
    assert start == "2026-09-07T08:18:00Z"
    assert end == "2026-09-07T08:24:00Z"
    payload = logs_payload("pay-service")
    assert payload["kind"] == "incident_logs"
    assert len(payload["logs"]) == 6
    levels = {row["level"] for row in payload["logs"]}
    assert levels == {"INFO", "ERROR", "WARN"}
    messages = [row["message"] for row in payload["logs"]]
    assert any("db slow query" in message for message in messages)
    assert any("circuit breaker opened" in message for message in messages)


def test_order_service_scenario_is_evidence_sparse():
    assert logs_payload("order-service")["logs"] == []
    series = metrics_payload("order-service")["series"]
    assert {item["metric"] for item in series} == {"cpu_usage_pct", "qps"}
    cpu = next(item for item in series if item["metric"] == "cpu_usage_pct")
    values = [point["value"] for point in cpu["datapoints"]]
    assert max(values) <= 40, "T20260908 CPU must stay flat (insufficient evidence)"


# ---------------------------------------------------------------------------
# URI parsing (SDD §3.2.1/§3.2.5)
# ---------------------------------------------------------------------------


def test_parse_ticket_uri():
    uri = parse_incident_uri(f"incident://ticket/{INCIDENT_DEMO_TICKET_ID}")
    assert uri.kind == "ticket"
    assert uri.name == INCIDENT_DEMO_TICKET_ID


def test_parse_logs_and_metrics_uri_with_window():
    uri = parse_incident_uri(
        "incident://logs/pay-service?start_ts=2026-09-07T08:18:00Z&end_ts=2026-09-07T08:25:00Z"
    )
    assert (uri.kind, uri.name, uri.start_ts, uri.end_ts) == (
        "logs",
        "pay-service",
        "2026-09-07T08:18:00Z",
        "2026-09-07T08:25:00Z",
    )
    metrics = parse_incident_uri("incident://metrics/pay-service")
    assert metrics.kind == "metrics"
    assert metrics.start_ts is None


@pytest.mark.parametrize(
    "uri, code",
    [
        ("incident://ticket/UNKNOWN", "NOT_FOUND"),
        ("incident://ticket", "UNKNOWN_RESOURCE_TYPE"),
        ("incident://blob/pay-service", "UNKNOWN_RESOURCE_TYPE"),
        (
            "incident://logs/pay-service?start_ts=2026-09-07T08:18:00Z",
            "INVALID_TIME_FORMAT",
        ),
        ("incident://logs/pay-service?start_ts=nonsense&end_ts=x", "INVALID_TIME_FORMAT"),
        (
            "incident://logs/pay-service?start_ts=2026-09-07T09:00:00Z&end_ts=2026-09-07T08:00:00Z",
            "INVALID_TIME_FORMAT",
        ),
    ],
)
def test_parse_invalid_uris_raise_port_errors(uri: str, code: str):
    with pytest.raises(HostPortError) as exc:
        parse_incident_uri(uri)
    assert exc.value.code == code


def test_payload_unknown_ticket_raises():
    with pytest.raises(HostPortError) as exc:
        incident_payload(parse_incident_uri("incident://ticket/T99999999"))
    assert exc.value.code == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Canonical payload rendering (SDD §3.2.4)
# ---------------------------------------------------------------------------


def test_ticket_payload_schema():
    payload = incident_payload(parse_incident_uri(f"incident://ticket/{INCIDENT_DEMO_TICKET_ID}"))
    assert payload["kind"] == "incident_ticket"
    assert payload["ticket_id"] == INCIDENT_DEMO_TICKET_ID
    assert payload["affected_services"] == ["pay-service"]
    assert payload["occurred_at"] == "2026-09-07T08:20:00Z"


def test_logs_payload_respects_window():
    payload = logs_payload(
        "pay-service",
        start_ts="2026-09-07T08:22:00Z",
        end_ts="2026-09-07T08:23:00Z",
    )
    timestamps = [row["timestamp"] for row in payload["logs"]]
    assert timestamps == ["2026-09-07T08:22:10Z", "2026-09-07T08:23:00Z"]


def test_metrics_payload_schema():
    payload = metrics_payload("pay-service")
    assert payload["kind"] == "incident_metrics"
    by_name = {item["metric"]: item for item in payload["series"]}
    cpu = by_name["cpu_usage_pct"]["datapoints"]
    assert cpu[1] == {"ts": "2026-09-07T08:20:00Z", "value": 86.0}


# ---------------------------------------------------------------------------
# ResourceResolver behaviour (SDD §3.2)
# ---------------------------------------------------------------------------


def test_resolve_ticket_metadata():
    resolved = _run(
        IncidentResources().resolve(_Ctx(), f"incident://ticket/{INCIDENT_DEMO_TICKET_ID}")
    )
    assert resolved.resource_ref == f"incident://ticket/{INCIDENT_DEMO_TICKET_ID}"
    assert resolved.canonical_id == "incident-ticket"
    assert resolved.classification == "incident"
    assert resolved.tenant_id == "tenant-a"
    assert resolved.digest.startswith("sha256:")


def test_resolve_unknown_ticket_fails_fast():
    with pytest.raises(HostPortError) as exc:
        _run(IncidentResources().resolve(_Ctx(), "incident://ticket/T99999999"))
    assert exc.value.code == "NOT_FOUND"


def test_resolve_non_incident_without_fallback_raises():
    with pytest.raises(HostPortError):
        _run(IncidentResources().resolve(_Ctx(), "synthetic-case:case-001"))


async def _synthetic_fallback(ctx, resource_ref):
    from enterprise_agent_platform.integration.host import ResolvedResource

    return ResolvedResource(
        resource_ref=resource_ref,
        canonical_id="synthetic-case",
        tenant_id="tenant-a",
        owner_id="reference-local-team",
        classification="synthetic",
        version="reference-resource/v1",
        digest="sha256:test",
    )


def test_resolve_delegates_non_incident_to_fallback():
    class _FallbackResolver:
        async def resolve(self, ctx, resource_ref):
            return await _synthetic_fallback(ctx, resource_ref)

    resolved = _run(
        IncidentResources(fallback=_FallbackResolver()).resolve(_Ctx(), "synthetic-case:case-001")
    )
    assert resolved.classification == "synthetic"


def test_read_content_returns_agent_visible_json():
    text = _run(IncidentResources().read_content(f"incident://ticket/{INCIDENT_DEMO_TICKET_ID}"))
    assert text is not None
    payload = json.loads(text)
    assert payload["kind"] == "incident_ticket"
    assert payload["severity"] == "P2"


def test_read_content_error_payload_for_unknown_ticket():
    text = _run(IncidentResources().read_content("incident://ticket/T99999999"))
    payload = json.loads(text)
    assert payload == {"kind": "error", "code": "NOT_FOUND", "message": payload["message"]}
    assert "not found" in payload["message"]


def test_read_content_none_for_non_incident():
    assert _run(IncidentResources().read_content("synthetic-case:case-001")) is None


# ---------------------------------------------------------------------------
# Workflow registration (contracts/commands.py)
# ---------------------------------------------------------------------------


def test_incident_workflow_registered():
    assert "it-incident-investigate" in WORKFLOW_PARAMETER_MODELS


def test_create_run_accepts_incident_workflow_without_parameters():
    command = CreateRunCommand(
        workflow_type="it-incident-investigate",
        intent="分析工单#T20260907",
        resource_refs=(f"incident://ticket/{INCIDENT_DEMO_TICKET_ID}",),
    )
    assert command.workflow_type == "it-incident-investigate"


def test_create_run_rejects_unknown_incident_parameters():
    with pytest.raises(ValueError):
        CreateRunCommand(
            workflow_type="it-incident-investigate",
            intent="分析工单",
            resource_refs=("incident://ticket/T20260907",),
            parameters={"mystery_knob": 1},
        )


# ---------------------------------------------------------------------------
# Chat intent mapping (control/chat.py)
# ---------------------------------------------------------------------------


def test_classify_incident_message_selects_incident_workflow():
    plan = classify_intent("请帮我分析工单#T20260907 的支付故障，输出根因")
    assert plan.workflow_type == "it-incident-investigate"


def test_classify_generic_message_keeps_synthetic_default():
    plan = classify_intent("Analyze failure patterns and summarize")
    assert plan.workflow_type == "synthetic-analysis"


def test_extract_ticket_id_from_text():
    assert extract_incident_ticket_id("请分析 工单T20260908 的问题") == "T20260908"
    assert extract_incident_ticket_id("ticket: t20260907 investigate") == "T20260907"
    assert extract_incident_ticket_id("没有任何票据的普通消息") is None


def test_chat_resources_derived_for_incident():
    plan = classify_intent("分析工单#T20260908，为何查询失败")
    refs = resolve_chat_resources(
        plan, "分析工单#T20260908，为何查询失败", ("synthetic-case:demo",)
    )
    assert refs == ("incident://ticket/T20260908",)


def test_chat_resources_fallback_demo_ticket():
    plan = classify_intent("有个 incident 需要排查一下")
    refs = resolve_chat_resources(plan, "有个 incident 需要排查一下", ("synthetic-case:demo",))
    assert refs == (f"incident://ticket/{INCIDENT_DEMO_TICKET_ID}",)


def test_chat_resources_preserves_explicit_incident_refs():
    plan = classify_intent("分析工单#T20260907")
    refs = resolve_chat_resources(plan, "分析工单#T20260907", ("incident://ticket/T20260908",))
    assert refs == ("incident://ticket/T20260908",)


def test_chat_resources_leaves_non_incident_untouched():
    plan = classify_intent("Analyze failure patterns")
    refs = resolve_chat_resources(plan, "Analyze failure patterns", ("synthetic-case:case-42",))
    assert refs == ("synthetic-case:case-42",)
