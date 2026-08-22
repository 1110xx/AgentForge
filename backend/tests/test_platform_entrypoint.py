"""Entrypoint composition: health routes are always mounted (L3 gate fix)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from enterprise_agent_platform.platform.entrypoint import create_app_from_env


def test_create_app_from_env_mounts_health_routes(monkeypatch) -> None:
    monkeypatch.setenv(
        "AGENT_PLATFORM_CONTAINER_FACTORY",
        "enterprise_agent_platform.reference.k8s_container:create_container",
    )
    monkeypatch.setenv("AGENT_PLATFORM_STORE", "memory")
    app = create_app_from_env()
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/api/agent-platform/v1/health/live" in paths
    assert "/api/agent-platform/v1/health/ready" in paths
    with TestClient(app) as client:
        live = client.get("/api/agent-platform/v1/health/live")
        assert live.status_code == 200, live.text
        ready = client.get("/api/agent-platform/v1/health/ready")
        assert ready.status_code == 200, ready.text


def test_create_app_from_env_rejects_factory_without_colon(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_PLATFORM_CONTAINER_FACTORY", "no-colon-here")
    try:
        create_app_from_env()
    except RuntimeError as error:
        assert "module:callable" in str(error)
    else:  # pragma: no cover - the factory contract must fail closed
        raise AssertionError("create_app_from_env accepted a malformed factory target")