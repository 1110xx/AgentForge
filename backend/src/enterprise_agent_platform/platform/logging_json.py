"""Structured JSON logging for log centralization (SDD G.4 ③ / ④).

One JSON object per log line on stdout/stderr, with correlation fields
(trace_id / run_id / attempt_id) injected from process-local context vars.
Container stdout is picked up by the observability agent (collector filelog /
promtail) and pushed to Loki, where ``trace_id`` becomes the join key between
traces (Tempo) and logs — the SDD's "traces↔logs 一 key 串联".

Usage::

    from enterprise_agent_platform.platform import logging_json

    logging_json.install_json_logs(level="INFO")
    with logging_json.correlation(trace_id=..., run_id=..., attempt_id=...):
        logger.info("bootstrap ok")   # → {"ts":..., "level":"INFO", ...}
"""
from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_corr_trace_id: ContextVar[str | None] = ContextVar("log_trace_id", default=None)
_corr_run_id: ContextVar[str | None] = ContextVar("log_run_id", default=None)
_corr_attempt_id: ContextVar[str | None] = ContextVar("log_attempt_id", default=None)


@contextmanager
def correlation(
    *,
    trace_id: str | None = None,
    run_id: str | None = None,
    attempt_id: str | None = None,
) -> Iterator[None]:
    """Run a block with correlation context; previous values are restored."""
    tokens: list[tuple[ContextVar[str | None], str | None]] = []
    if trace_id is not None:
        tokens.append((_corr_trace_id, _corr_trace_id.set(trace_id)))
    if run_id is not None:
        tokens.append((_corr_run_id, _corr_run_id.set(run_id)))
    if attempt_id is not None:
        tokens.append((_corr_attempt_id, _corr_attempt_id.set(attempt_id)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def set_trace_id(trace_id: str) -> None:
    _corr_trace_id.set(trace_id)


def set_run_id(run_id: str) -> None:
    _corr_run_id.set(run_id)


def set_attempt_id(attempt_id: str) -> None:
    _corr_attempt_id.set(attempt_id)


class JsonLogFormatter(logging.Formatter):
    """Emit each record as a single JSON object (safe scalar fields only)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str | int | float] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        trace_id = _corr_trace_id.get()
        run_id = _corr_run_id.get()
        attempt_id = _corr_attempt_id.get()
        if trace_id:
            payload["trace_id"] = trace_id
        if run_id:
            payload["run_id"] = run_id
        if attempt_id:
            payload["attempt_id"] = attempt_id
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def install_json_logs_if_enabled() -> None:
    """Install JSON logging when ``AGENT_PLATFORM_JSON_LOGS=1`` (env-gated)."""
    import os

    if os.environ.get("AGENT_PLATFORM_JSON_LOGS", "").lower() in {"1", "true", "yes", "on"}:
        install_json_logs(os.environ.get("AGENT_PLATFORM_LOG_LEVEL", "INFO"))


def install_json_logs(level: str = "INFO") -> None:
    """Replace the root logging configuration with JSON lines to stderr."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLogFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    # uvicorn/access logs are children of uvicorn loggers, not root: point them
    # at the same JSON handler so container log lines stay homogeneous.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.addHandler(handler)
        logger.propagate = False
    logging.getLogger(__name__).debug("JSON logging installed")


__all__ = [
    "JsonLogFormatter",
    "correlation",
    "install_json_logs",
    "install_json_logs_if_enabled",
    "set_attempt_id",
    "set_run_id",
    "set_trace_id",
]