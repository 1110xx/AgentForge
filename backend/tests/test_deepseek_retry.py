"""Transient-network retry budget for the DeepSeek provider.

Egress paths on some hosts (WSL2/Docker NAT) intermittently reset TLS
mid-handshake; a bounded retry loop over fresh connections makes model
calls resilient without masking 4xx auth/validation errors.
"""
from __future__ import annotations

import json

import httpx
import pytest

from enterprise_agent_platform.reference.deepseek_provider import (
    DeepSeekModelSessionProvider,
)

_FAKE_KEY = "sk-test-not-a-real-key"


def _provider(
    *, transport: httpx.AsyncBaseTransport, attempts: int = 3
) -> DeepSeekModelSessionProvider:
    provider = DeepSeekModelSessionProvider(
        api_key=_FAKE_KEY,
        base_url="https://example.test/v1",
        retry_attempts=attempts,
    )
    # Inject a transport-backed client so no real network is touched.
    provider._client = httpx.AsyncClient(transport=transport)  # type: ignore[attr-defined]
    return provider


def _ok_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "hello from the model"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


@pytest.mark.asyncio
async def test_retries_transient_errors_then_succeeds() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) < 3:
            raise httpx.ReadError("TLS EOF mid-handshake (simulated)")
        return _ok_response()

    provider = _provider(transport=httpx.MockTransport(handler))
    text = await provider._call_api([{"role": "user", "content": "hi"}])
    assert text == "hello from the model"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_fails_fast_on_http_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider = _provider(transport=httpx.MockTransport(handler))
    with pytest.raises(Exception, match="401"):
        await provider._call_api([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_retries_http_503() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 2:
            return httpx.Response(503, text="upstream warming up")
        return _ok_response()

    provider = _provider(transport=httpx.MockTransport(handler))
    text = await provider._call_api([{"role": "user", "content": "hi"}])
    assert text == "hello from the model"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_tools_endpoint_retries_too() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ConnectError("connection reset (simulated)")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "using a tool", "tool_calls": []},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    provider = _provider(transport=httpx.MockTransport(handler))
    out = await provider._call_api_tools(
        [{"role": "user", "content": "hi"}],
        [{"name": "lookup", "description": "d", "parameters": json.dumps({"type": "object"})}],
    )
    assert out["stop_reason"] == "end_turn"
    assert len(calls) == 2
