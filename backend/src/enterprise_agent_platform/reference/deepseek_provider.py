"""DeepSeek v4 Flash-based model provider for production use.

One session per Run. Integrates with DeepSeek API for real LLM capabilities.
Reads model config from config.toml / environment for flexible user customization.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx
from enterprise_agent_platform.execution.session import (
    FollowupExchange,
    SessionHandle,
    SessionProviderError,
)
from enterprise_agent_platform.platform.config_reader import (
    AppConfig,
    ConfigReader,
    ProviderParameters,
    SessionConfig,
)


@dataclass(slots=True)
class _DeepSeekSession:
    """Session state for a single Run with DeepSeek."""

    run_id: str
    intent: str
    model: str = "deepseek-chat"
    max_history_length: int = 100
    messages: List[Dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.messages = [
            {
                "role": "system",
                "content": (
                    f"You are a professional enterprise AI assistant. "
                    f"Task intent: \"{self.intent}\".\n\n"
                    "Complete the task thoroughly, then answer follow-up "
                    "questions based on the task memories. Be precise and factual."
                ),
            }
        ]


class DeepSeekModelSessionProvider:
    """Production-ready DeepSeek v4 Flash model provider through session seam."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        *,
        app_config: Optional[AppConfig] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        timeout_seconds: Optional[int] = None,
        max_history_length: Optional[int] = None,
        read_only_followup: Optional[bool] = None,
    ):
        # Load config if not provided
        if app_config is None:
            reader = ConfigReader()
            app_config = reader.read()
        self._app_config = app_config

        # Resolve API settings: explicit arg > config.toml > env var > default
        self.api_key = api_key or app_config.resolve_api_key() or ""
        self.base_url = (
            base_url
            or app_config.resolve_base_url()
            or "https://api.deepseek.com/v1"
        )

        # Model parameters: explicit arg > config.toml > hardcoded default
        params = app_config.parameters
        self.model = model or app_config.provider.model or "deepseek-chat"
        self.temperature = temperature if temperature is not None else params.temperature
        self.max_tokens = max_tokens if max_tokens is not None else params.max_tokens
        self.top_p = top_p if top_p is not None else params.top_p
        self.frequency_penalty = (
            frequency_penalty if frequency_penalty is not None else params.frequency_penalty
        )
        self.presence_penalty = (
            presence_penalty if presence_penalty is not None else params.presence_penalty
        )
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else params.timeout_seconds
        )

        # Session config
        sess_cfg = app_config.session
        self.max_history_length = (
            max_history_length if max_history_length is not None else sess_cfg.max_history_length
        )
        self.read_only_followup = (
            read_only_followup if read_only_followup is not None else sess_cfg.read_only_followup
        )

        # Runtime state
        self._sessions: Dict[str, _DeepSeekSession] = {}
        self._closed: set[str] = set()
        self._client: httpx.AsyncClient | None = None  # created lazily in _call_api

    async def open(
        self,
        *,
        run_id: str,
        intent: str,
        resource_refs: tuple[str, ...],
        host_context_ref: str | None,
    ) -> SessionHandle:
        """Open (or return) the session bound to this Run."""
        session_id = f"session:{run_id}"
        if session_id in self._sessions and session_id not in self._closed:
            raise SessionProviderError("SESSION_ALREADY_OPEN", "one session per Run")
        # Clear the closed flag so _require_open works with the new session
        self._closed.discard(session_id)

        self._sessions[session_id] = _DeepSeekSession(
            run_id=run_id,
            intent=intent,
            model=self.model,
            max_history_length=self.max_history_length,
        )
        return SessionHandle(session_id=session_id, run_id=run_id)

    async def run_task(self, handle: SessionHandle) -> None:
        """Drive the task loop inside the session."""
        session = self._require_open(handle)

        task_message = {
            "role": "user",
            "content": (
                f"Please execute the task: \"{session.intent}\".\n\n"
                "1. Analyze the requirements\n"
                "2. Execute step by step\n"
                "3. Summarize the results\n"
                "4. Note any important findings"
            ),
        }
        session.messages.append(task_message)

        try:
            response = await self._call_api(session.messages)
            session.messages.append({"role": "assistant", "content": response})
        except Exception as e:
            session.messages.append(
                {"role": "assistant", "content": f"Error: {e}"}
            )
            raise SessionProviderError("TASK_EXECUTION_FAILED", str(e))

    async def followup(
        self,
        handle: SessionHandle,
        message: str,
        *,
        read_only: bool = True,
    ) -> str:
        """Append a user message to the session and return the model's answer."""
        session = self._require_open(handle)

        # Enforce read-only guardrail
        if self.read_only_followup and not read_only:
            raise SessionProviderError(
                "WRITE_NOT_ALLOWED",
                "Follow-up is read-only by platform policy",
            )

        # Check history limit
        if len(session.messages) >= session.max_history_length:
            raise SessionProviderError(
                "SESSION_HISTORY_LIMIT",
                f"Session history exceeded max {session.max_history_length} messages",
            )

        session.messages.append({"role": "user", "content": message})

        try:
            response = await self._call_api(session.messages)
            session.messages.append({"role": "assistant", "content": response})
            return response
        except Exception as e:
            error_msg = f"Follow-up failed: {e}"
            session.messages.append({"role": "assistant", "content": error_msg})
            raise SessionProviderError("FOLLOWUP_FAILED", str(e))

    async def close(self, handle: SessionHandle) -> None:
        """Close the session bound to this Run."""
        self._require_open(handle)
        self._closed.add(handle.session_id)

    def _require_open(self, handle: SessionHandle) -> _DeepSeekSession:
        """Ensure session exists and is open, return it."""
        if handle.session_id not in self._sessions:
            raise SessionProviderError("SESSION_NOT_FOUND", "session was not found")
        if handle.session_id in self._closed:
            raise SessionProviderError("SESSION_CLOSED", "session is closed")
        return self._sessions[handle.session_id]

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return a reusable HTTP client, recreating if closed."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds, connect=10.0),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
            )
        return self._client

    async def _call_api(self, messages: List[Dict[str, str]]) -> str:
        """Call the DeepSeek chat completions API with configured parameters."""
        client = await self._ensure_client()
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
        }

        response = await client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
        )

        if response.status_code != 200:
            raise SessionProviderError(
                "API_CALL_FAILED",
                f"DeepSeek API error {response.status_code}: {response.text}",
            )

        return response.json()["choices"][0]["message"]["content"]

    async def _call_api_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Call the LLM API with tool definitions support.

        Returns the full response dict including content blocks (text, tool_use)
        and stop_reason. Used by pi-agent-core integration for tool-calling agents.
        """
        client = await self._ensure_client()
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
        }
        if tools:
            # Convert pi-agent-core tool format to OpenAI-compatible format
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}, "required": []}),
                    },
                }
                for t in tools
            ]

        response = await client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
        )

        if response.status_code != 200:
            raise SessionProviderError(
                "API_CALL_FAILED",
                f"DeepSeek API error {response.status_code}: {response.text}",
            )

        data = response.json()
        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice.get("finish_reason", "stop")

        # Convert stop_reason to pi-agent-core format
        stop_reason_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
        }
        mapped_reason = stop_reason_map.get(finish_reason, finish_reason)

        # Parse content blocks
        content_blocks: List[Dict[str, Any]] = []
        if message.get("content"):
            content_blocks.append({
                "type": "text",
                "text": message["content"],
            })

        # Parse tool calls
        tool_calls = message.get("tool_calls", [])
        for tc in tool_calls:
            function_data = tc.get("function", {})
            try:
                arguments = json.loads(function_data.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            content_blocks.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": function_data.get("name", ""),
                "input": arguments,
            })

        # Usage info
        usage = data.get("usage", {})

        return {
            "content": content_blocks,
            "stop_reason": mapped_reason,
            "usage": {
                "input": usage.get("prompt_tokens", 0),
                "output": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

    async def __aenter__(self) -> "DeepSeekModelSessionProvider":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        for session_id in list(self._sessions.keys()):
            try:
                await self.close(
                    SessionHandle(session_id=session_id, run_id="")
                )
            except Exception:
                pass
        if self._client is not None:
            await self._client.aclose()


def create_deepseek_provider(
    api_key: Optional[str] = None,
    config_path: Optional[str] = None,
) -> DeepSeekModelSessionProvider:
    """Create a DeepSeek provider from config.toml and optional overrides."""
    reader = ConfigReader(config_path)
    app_config = reader.read()
    return DeepSeekModelSessionProvider(api_key=api_key, app_config=app_config)


__all__ = ["DeepSeekModelSessionProvider", "create_deepseek_provider"]