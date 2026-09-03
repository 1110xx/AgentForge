"""Env-driven run-session provider selection for the deployed K8s API.

``AGENT_PLATFORM_DEEPSEEK_API_KEY`` present → real DeepSeek provider (the
cluster's attempt pods proxy model calls over the internal
``/runtime/model-call`` endpoint, so this factory is the single point that
flips the cluster onto a real model). Absent → the deterministic in-memory
stub, preserving demo/gate behavior with zero external calls.
"""
from __future__ import annotations

from enterprise_agent_platform.reference import (
    DeepSeekModelSessionProvider,
    InMemoryRunSessionProvider,
)
from enterprise_agent_platform.reference.k8s_container import _resolve_run_sessions


def test_no_key_selects_in_memory_stub(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_PLATFORM_DEEPSEEK_API_KEY", raising=False)
    provider = _resolve_run_sessions(None)  # telemetry None -> passthrough
    assert isinstance(provider, InMemoryRunSessionProvider)


def test_key_selects_deepseek_provider(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_PLATFORM_DEEPSEEK_API_KEY", "sk-test-not-a-real-key")
    provider = _resolve_run_sessions(None)
    assert isinstance(provider, DeepSeekModelSessionProvider)
    assert provider.api_key == "sk-test-not-a-real-key"
    assert provider.base_url == "https://api.deepseek.com/v1"
    assert provider.model == "deepseek-chat"


def test_key_honors_model_override(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_PLATFORM_DEEPSEEK_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("AGENT_PLATFORM_DEFAULT_MODEL", "deepseek-reasoner")
    provider = _resolve_run_sessions(None)
    assert isinstance(provider, DeepSeekModelSessionProvider)
    assert provider.model == "deepseek-reasoner"
