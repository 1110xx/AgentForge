"""IT incident investigation business vertical (``it-incident-investigate``).

Reference-only deterministic business domain for the B-档 MVP: an incident
ticket plus its log/metric evidence are served through the platform's
``ResourceResolver`` protocol under the ``incident://`` scheme (SDD
it-incident-investigate §3.2). Everything in this module is pure data + pure
parsing — no agent/kernel code — so unit tests stay hermetic, and the data
source can be swapped for a PG / real-incident-system adapter behind the same
URI contract in production (§3.2.3 adapter seam).

Dataset follows SDD §4.4:
* ``T20260907`` — full evidence scenario (pay-service DB slow-query root cause);
* ``T20260908`` — insufficient-evidence scenario (order-service, empty logs).

Timestamps are UTC ISO-8601 strings (``...Z``) so range filtering stays
string-comparable after the entries are stored already sorted.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.integration.host import (
    HostPortError,
    ResolvedResource,
    ResourceResolver,
)

# ---------------------------------------------------------------------------
# URI protocol
# ---------------------------------------------------------------------------

INCIDENT_SCHEME = "incident"
INCIDENT_URI_PREFIX = "incident://"
INCIDENT_DEMO_TICKET_ID = "T20260907"

_INCIDENT_URI = re.compile(
    r"^incident://(?P<kind>ticket|logs|metrics)/(?P<name>[A-Za-z0-9_.-]+)"
    r"(?:\?(?P<query>[^#]*))?$"
)

_INCIDENT_TICKET_ID = re.compile(r"^T\d{6,}$")
_SERVICE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")

_EPOCH = datetime.fromisoformat("1970-01-01T00:00:00+00:00")


def _parse_iso(value: str, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise HostPortError(code, f"invalid ISO8601 timestamp: {value}") from error
    if parsed.tzinfo is None:
        raise HostPortError(code, f"timestamp must carry a timezone: {value}")
    return parsed.astimezone(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class IncidentUri:
    kind: str  # ticket | logs | metrics
    name: str
    start_ts: str | None = None
    end_ts: str | None = None


def parse_incident_uri(resource_ref: str) -> IncidentUri:
    """Parse an ``incident://`` reference into a typed read request.

    Raises ``HostPortError`` with the SDD error codes (UNKNOWN_RESOURCE_TYPE /
    INVALID_TIME_FORMAT / NOT_FOUND) when the URI is malformed.
    """
    if not resource_ref.startswith(INCIDENT_URI_PREFIX):
        raise HostPortError("UNKNOWN_RESOURCE_TYPE", "resource is not an incident URI")
    match = _INCIDENT_URI.match(resource_ref)
    if match is None:
        raise HostPortError("UNKNOWN_RESOURCE_TYPE", f"malformed incident URI: {resource_ref}")
    kind, name, raw_query = match.group("kind"), match.group("name"), match.group("query")
    if kind not in ("ticket", "logs", "metrics"):
        raise HostPortError("UNKNOWN_RESOURCE_TYPE", f"unknown incident resource kind: {kind}")
    if kind == "ticket" and not _INCIDENT_TICKET_ID.fullmatch(name):
        raise HostPortError("NOT_FOUND", f"invalid ticket id: {name}")
    if kind in ("logs", "metrics") and not _SERVICE_NAME.fullmatch(name):
        raise HostPortError("NOT_FOUND", f"invalid service name: {name}")
    query: dict[str, str] = {}
    if raw_query:
        for pair in raw_query.split("&"):
            key, _, value = pair.partition("=")
            if not key:
                continue
            query[key] = value
    start_ts = query.get("start_ts")
    end_ts = query.get("end_ts")
    if kind in ("logs", "metrics"):
        # Both bounds optional: a single bound or malformed values are errors;
        # when neither is given the resolver falls back to the service window.
        if (start_ts is None) != (end_ts is None):
            raise HostPortError("INVALID_TIME_FORMAT", "start_ts and end_ts must be given together")
        for bound in (start_ts, end_ts):
            if bound is not None:
                _parse_iso(bound, "INVALID_TIME_FORMAT")
        if start_ts and end_ts and start_ts > end_ts:
            raise HostPortError("INVALID_TIME_FORMAT", "start_ts must not exceed end_ts")
    return IncidentUri(kind=kind, name=name, start_ts=start_ts, end_ts=end_ts)


# ---------------------------------------------------------------------------
# Dataset (SDD §4.2–4.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IncidentTicket:
    ticket_id: str
    title: str
    description: str
    occurred_at: str
    affected_services: tuple[str, ...]
    severity: str
    submitter: str


@dataclass(frozen=True, slots=True)
class IncidentLogEntry:
    timestamp: str
    level: str
    message: str


@dataclass(frozen=True, slots=True)
class IncidentMetricSeries:
    metric: str
    datapoints: tuple[tuple[str, float], ...] = field(default_factory=tuple)


_INCIDENT_TICKETS: dict[str, IncidentTicket] = {}


def _register_ticket(ticket: IncidentTicket) -> None:
    _INCIDENT_TICKETS[ticket.ticket_id] = ticket


_register_ticket(
    IncidentTicket(
        ticket_id="T20260907",
        title="支付服务大量接口504超时",
        description="用户下单支付接口频繁504，部分交易失败，影响支付链路",
        occurred_at="2026-09-07T08:20:00Z",
        affected_services=("pay-service",),
        severity="P2",
        submitter="monitor-alarm",
    )
)
_register_ticket(
    IncidentTicket(
        ticket_id="T20260908",
        title="用户订单查询偶发失败",
        description="部分用户反馈订单查询偶发失败，无固定触发规律",
        occurred_at="2026-09-07T10:05:00Z",
        affected_services=("order-service",),
        severity="P3",
        submitter="customer-support",
    )
)


def incident_tickets() -> tuple[IncidentTicket, ...]:
    return tuple(_INCIDENT_TICKETS.values())


def get_incident_ticket(ticket_id: str) -> IncidentTicket | None:
    return _INCIDENT_TICKETS.get(ticket_id)


# (service, level, timestamp, message)
_RAW_LOGS: tuple[tuple[str, str, str, str], ...] = (
    ("pay-service", "INFO", "2026-09-07T08:19:30Z", "service started, health check ok"),
    (
        "pay-service",
        "ERROR",
        "2026-09-07T08:21:12Z",
        "db slow query, query_cost=7.8s, sql=select * from order where user_id=?",
    ),
    (
        "pay-service",
        "ERROR",
        "2026-09-07T08:21:30Z",
        "db slow query, query_cost=8.2s, sql=select * from order where user_id=?",
    ),
    ("pay-service", "WARN", "2026-09-07T08:21:45Z", "http gateway upstream timeout"),
    (
        "pay-service",
        "ERROR",
        "2026-09-07T08:22:10Z",
        "request timeout after 30s, path=/api/pay/create",
    ),
    ("pay-service", "WARN", "2026-09-07T08:23:00Z", "circuit breaker opened for db-primary"),
)
# (service, metric, ts, value)
_RAW_METRICS: tuple[tuple[str, str, str, float], ...] = (
    ("pay-service", "cpu_usage_pct", "2026-09-07T08:18:00Z", 42.0),
    ("pay-service", "cpu_usage_pct", "2026-09-07T08:20:00Z", 86.0),
    ("pay-service", "cpu_usage_pct", "2026-09-07T08:22:00Z", 92.0),
    ("pay-service", "cpu_usage_pct", "2026-09-07T08:24:00Z", 78.0),
    ("pay-service", "qps", "2026-09-07T08:18:00Z", 1180.0),
    ("pay-service", "qps", "2026-09-07T08:20:00Z", 1240.0),
    ("pay-service", "qps", "2026-09-07T08:22:00Z", 1310.0),
    ("pay-service", "qps", "2026-09-07T08:24:00Z", 1290.0),
    # order-service (T20260908): deliberately flat / no error logs
    ("order-service", "cpu_usage_pct", "2026-09-07T10:00:00Z", 34.0),
    ("order-service", "cpu_usage_pct", "2026-09-07T10:05:00Z", 33.0),
    ("order-service", "cpu_usage_pct", "2026-09-07T10:10:00Z", 38.0),
    ("order-service", "cpu_usage_pct", "2026-09-07T10:15:00Z", 36.0),
    ("order-service", "qps", "2026-09-07T10:00:00Z", 612.0),
    ("order-service", "qps", "2026-09-07T10:05:00Z", 624.0),
    ("order-service", "qps", "2026-09-07T10:10:00Z", 601.0),
    ("order-service", "qps", "2026-09-07T10:15:00Z", 618.0),
)

_LOG_ROWS: dict[str, tuple[IncidentLogEntry, ...]] = {}
_METRIC_SERIES: dict[str, tuple[IncidentMetricSeries, ...]] = {}
_SERVICE_WINDOWS: dict[str, tuple[str, str]] = {}

for _svc, _level, _ts, _msg in _RAW_LOGS:
    _LOG_ROWS.setdefault(_svc, []).append(
        IncidentLogEntry(timestamp=_ts, level=_level, message=_msg)
    )
for _svc, _rows in _LOG_ROWS.items():
    _LOG_ROWS[_svc] = tuple(sorted(_rows, key=lambda row: row.timestamp))

_metric_points: dict[str, dict[str, list[tuple[str, float]]]] = {}
for _svc, _metric, _ts, _value in _RAW_METRICS:
    _metric_points.setdefault(_svc, {}).setdefault(_metric, []).append((_ts, _value))
for _svc, series in _metric_points.items():
    ordered = tuple(
        IncidentMetricSeries(metric=metric, datapoints=tuple(sorted(points)))
        for metric, points in sorted(series.items())
    )
    _METRIC_SERIES[_svc] = ordered
for _svc, _series in _METRIC_SERIES.items():
    metric_ts = [ts for item in _series for ts, _ in item.datapoints]
    log_ts = [row.timestamp for row in _LOG_ROWS.get(_svc, ())]
    all_ts = metric_ts + log_ts
    _SERVICE_WINDOWS[_svc] = (min(all_ts), max(all_ts))


def _service_logs(service: str) -> tuple[IncidentLogEntry, ...]:
    return _LOG_ROWS.get(service, ())


def _service_metrics(service: str) -> tuple[IncidentMetricSeries, ...]:
    return _METRIC_SERIES.get(service, ())


def service_window(service: str) -> tuple[str, str]:
    if service not in _SERVICE_WINDOWS:
        raise HostPortError("NOT_FOUND", f"unknown incident service: {service}")
    return _SERVICE_WINDOWS[service]


# ---------------------------------------------------------------------------
# Canonical payload rendering (SDD §3.2.4 response schemas)
# ---------------------------------------------------------------------------


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _checksum(value: dict[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def ticket_payload(ticket: IncidentTicket) -> dict[str, Any]:
    return {
        "kind": "incident_ticket",
        "ticket_id": ticket.ticket_id,
        "title": ticket.title,
        "description": ticket.description,
        "occurred_at": ticket.occurred_at,
        "affected_services": list(ticket.affected_services),
        "severity": ticket.severity,
        "submitter": ticket.submitter,
    }


def _within(ts: str, start: datetime, end: datetime) -> bool:
    parsed = _parse_iso(ts, "INVALID_TIME_FORMAT")
    return start <= parsed <= end


def logs_payload(
    service: str, start_ts: str | None = None, end_ts: str | None = None
) -> dict[str, Any]:
    rows = _service_logs(service)
    start_bound, end_bound = service_window(service)
    start = _parse_iso(start_ts or start_bound, "INVALID_TIME_FORMAT")
    end = _parse_iso(end_ts or end_bound, "INVALID_TIME_FORMAT")
    selected = [
        {"timestamp": row.timestamp, "level": row.level, "message": row.message}
        for row in rows
        if _within(row.timestamp, start, end)
    ]
    return {
        "kind": "incident_logs",
        "service_name": service,
        "time_range": {"start_ts": _iso(start), "end_ts": _iso(end)},
        "logs": selected,
    }


def metrics_payload(
    service: str, start_ts: str | None = None, end_ts: str | None = None
) -> dict[str, Any]:
    series = _service_metrics(service)
    start_bound, end_bound = service_window(service)
    start = _parse_iso(start_ts or start_bound, "INVALID_TIME_FORMAT")
    end = _parse_iso(end_ts or end_bound, "INVALID_TIME_FORMAT")
    selected = [
        {
            "metric": item.metric,
            "datapoints": [
                {"ts": ts, "value": value}
                for ts, value in item.datapoints
                if _within(ts, start, end)
            ],
        }
        for item in series
    ]
    selected = [item for item in selected if item["datapoints"]]
    return {
        "kind": "incident_metrics",
        "service_name": service,
        "time_range": {"start_ts": _iso(start), "end_ts": _iso(end)},
        "series": selected,
    }


def error_payload(code: str, message: str) -> dict[str, str]:
    return {"kind": "error", "code": code, "message": message}


def incident_payload(uri: IncidentUri) -> dict[str, Any]:
    """Resolve an incident URI into its canonical business payload."""
    if uri.kind == "ticket":
        ticket = get_incident_ticket(uri.name)
        if ticket is None:
            raise HostPortError("NOT_FOUND", f"incident ticket not found: {uri.name}")
        return ticket_payload(ticket)
    if uri.kind == "logs":
        return logs_payload(uri.name, uri.start_ts, uri.end_ts)
    if uri.kind == "metrics":
        return metrics_payload(uri.name, uri.start_ts, uri.end_ts)
    raise HostPortError("UNKNOWN_RESOURCE_TYPE", f"unknown incident resource kind: {uri.kind}")


def canonical_id_for(kind: str) -> str:
    return f"incident-{kind}"


# ---------------------------------------------------------------------------
# ResourceResolver implementation (SDD §3.2)
# ---------------------------------------------------------------------------


class IncidentResources:
    """ResourceResolver for ``incident://`` backed by the in-memory demo data.

    Non-incident references fall back to an optional inner resolver (the
    reference synthetic dataset) so demo containers expose both protocols.
    """

    RESOURCE_VERSION = "incident-resource/v1"
    CLASSIFICATION = "incident"
    # Scheme URIs the resolver owns: create-run authorization admits
    # ``incident://...`` references only because this resolver declares the
    # scheme (integration/host.py scheme gate).
    resource_schemes: tuple[str, ...] = (INCIDENT_SCHEME,)

    def __init__(self, fallback: ResourceResolver | None = None) -> None:
        self._fallback = fallback

    async def resolve(self, ctx: RequestContext, resource_ref: str) -> ResolvedResource:
        if not resource_ref.startswith(INCIDENT_URI_PREFIX):
            if self._fallback is not None:
                return await self._fallback.resolve(ctx, resource_ref)
            raise HostPortError("NOT_FOUND", f"reference resource was not found: {resource_ref}")
        uri = parse_incident_uri(resource_ref)
        payload = incident_payload(uri)
        return ResolvedResource(
            resource_ref=resource_ref,
            canonical_id=canonical_id_for(uri.kind),
            tenant_id=ctx.tenant_id,
            owner_id="incident-team",
            classification=self.CLASSIFICATION,
            version=self.RESOURCE_VERSION,
            digest=_checksum(payload),
        )

    async def read_content(self, resource_ref: str) -> str | None:
        """Render agent-visible JSON content for an incident reference.

        Invalid references resolve to the SDD ``kind: error`` payload instead of
        raising so a runtime read stays non-fatal for the Agent (it can read the
        missing-data note and keep going). Non-incident refs return None so the
        platform falls back to its existing read text.
        """
        if not resource_ref.startswith(INCIDENT_URI_PREFIX):
            return None
        try:
            uri = parse_incident_uri(resource_ref)
            payload = incident_payload(uri)
        except HostPortError as error:
            payload = error_payload(error.code, error.message)
        return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "INCIDENT_DEMO_TICKET_ID",
    "INCIDENT_SCHEME",
    "INCIDENT_URI_PREFIX",
    "IncidentLogEntry",
    "IncidentMetricSeries",
    "IncidentResources",
    "IncidentTicket",
    "IncidentUri",
    "canonical_id_for",
    "error_payload",
    "get_incident_ticket",
    "incident_payload",
    "incident_tickets",
    "logs_payload",
    "metrics_payload",
    "parse_incident_uri",
    "service_window",
    "ticket_payload",
]
