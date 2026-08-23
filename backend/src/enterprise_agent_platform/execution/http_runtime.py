"""Pod-side HTTP runtime transport (HttpRunner, Phase 3).

This is the HTTP sibling of ``subprocess_runtime.py``: instead of a JSON-line
pipe the K8s/Docker Runner drives the Control-Plane **Internal API** directly
(``AGENT_PLATFORM_CONTROL_PLANE_URL``). The K8s Job entry point is
``python -m enterprise_agent_platform.execution.runtime``, whose ``main()``
delegates here.

Capability tokens (demo projection, see fastapi/internal_adapter.py):

* bootstrap — Bearer ``projected:{tenant_id}`` (from ``AGENT_PLATFORM_BOOTSTRAP_TOKEN``)
* runtime ops — Bearer ``runtime-token:{attempt_id}`` (issued by bootstrap)

Lifecycle (mirrors the pipe flow, SDD §6.1):

    bootstrap (identity + **Lease activation**)
      → restore (checkpoint cursor + agent_state snapshot)
      → Agent.prompt via HttpStream (POST /runtime/model-call, proxy to the
        real LLM provider through the Control Plane's RunSessionProvider)
      → turn checkpoints (POST /runtime/checkpoints, TurnEnd snapshots)
      → commit_final (POST /runtime/checkpoints/final) — terminal transition
        happens control-plane-side, exactly like the pipe path
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
from pi_agent_core.types import (
    AgentContext,
    AssistantMessage,
    AssistantMessageEvent,
    Model,
    SimpleStreamOptions,
    StreamErrorEvent,
    StreamFn,
    StreamResult,
    StreamStartEvent,
)

from enterprise_agent_platform.execution.pipe_transport import (
    OP_PROPOSE_ACTION,
    OP_PUBLISH_ARTIFACT,
    OP_READ_TOOL,
)
from enterprise_agent_platform.execution.runtime import (
    AgentRuntime,
    BootstrapGrant,
    RuntimeCheckpoint,
    RuntimeContext,
)
from enterprise_agent_platform.execution.subprocess_runtime import _emit_response_events
from enterprise_agent_platform.tools.native import create_native_tools
from enterprise_agent_platform.tools.remote import create_remote_tools

logger = logging.getLogger(__name__)

_RUNTIME_HTTP_TIMEOUT = 60.0
OP_MODEL_CALL = "model_call"


def _restore_http_context(
    raw: dict[str, Any], previous: RuntimeContext
) -> RuntimeContext:
    """Rebuild the context from a heartbeat response, preserving the subject.

    The heartbeat handler returns only the lease facts (attempt/generation/
    pod_uid/runtime_token/lease_owner/lease_version); tenant/run/execution_unit
    are carried over from the pre-refresh context so the next CAS-signature
    request stays addressed to the same subject.
    """
    return RuntimeContext(
        attempt_id=str(raw["attempt_id"]),
        generation=int(raw["generation"]),
        pod_uid=str(raw.get("pod_uid", previous.pod_uid)),
        runtime_token=str(raw.get("runtime_token", previous.runtime_token)),
        lease_owner=str(raw.get("lease_owner", previous.lease_owner)),
        lease_version=int(raw.get("lease_version", previous.lease_version)),
        tenant_id=previous.tenant_id,
        run_id=previous.run_id,
        execution_unit_id=previous.execution_unit_id,
    )


class _EnvIdentityProvider:
    """Reads the demo bootstrap identity from the environment.

    The K8s worker injects ``AGENT_PLATFORM_BOOTSTRAP_TOKEN=projected:{tenant}``
    into the Job env; the Pod UID comes from ``AGENT_PLATFORM_POD_UID`` (downward
    API) or defaults to a stable local value.
    """

    async def provide(self) -> tuple[str, str]:
        token = os.environ.get("AGENT_PLATFORM_BOOTSTRAP_TOKEN", "").strip()
        pod_uid = os.environ.get("AGENT_PLATFORM_POD_UID", "").strip()
        if not token:
            raise RuntimeError("AGENT_PLATFORM_BOOTSTRAP_TOKEN is required in HTTP runtime mode")
        if not pod_uid:
            pod_uid = f"http-pod:{os.environ.get('AGENT_PLATFORM_ATTEMPT_ID', 'unknown')}"
        return token, pod_uid


class HttpRuntimeClient:
    """Single HTTP client for BootstrapClient + RuntimeControlClient + TransportClient.

    The class implements all three protocols the Runner needs:

    * ``claim`` — POST /internal/v1/runtime/bootstrap (activates the Lease)
    * ``restore`` / ``heartbeat`` / ``commit_checkpoint`` /
      ``commit_final_checkpoint`` / ``record_failure`` — control ops
    * ``request(op, kwargs)`` — remote tool transport (read_tool /
      publish_artifact / propose_action)

    The runtime subject (tenant/run/unit/attempt/generation) and the
    ``runtime-token`` are bound from the last ``RuntimeContext``.
    """

    def __init__(self, base_url: str, *, timeout: float = _RUNTIME_HTTP_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._context: RuntimeContext | None = None

    @property
    def context(self) -> RuntimeContext | None:
        return self._context

    def bind(self, context: RuntimeContext) -> None:
        self._context = context

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _client_for(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _post(self, path: str, body: dict[str, Any], *, token: str) -> dict[str, Any]:
        client = await self._client_for()
        try:
            response = await client.post(
                f"{self._base_url}{path}",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as error:
            raise RuntimeError(f"http runtime transport error on {path}: {error}") from error
        if response.status_code >= 400:
            message = ""
            try:
                payload = response.json()
                message = str(payload.get("message", ""))
                detail = payload.get("detail")
                if detail:
                    message = f"{message} detail={detail}"
            except Exception:  # noqa: BLE001 - error body is best-effort only
                logger.debug("non-JSON error body from %s", path)
            raise RuntimeError(
                f"http runtime {path} failed: HTTP {response.status_code} {message}".strip()
            )
        return dict(response.json())

    def _subject(self) -> dict[str, Any]:
        if self._context is None:
            raise RuntimeError("HttpRuntimeClient has no bound RuntimeContext")
        ctx = self._context
        return {
            "tenant_id": ctx.tenant_id,
            "run_id": ctx.run_id,
            "execution_unit_id": ctx.execution_unit_id,
            "attempt_id": ctx.attempt_id,
            "generation": ctx.generation,
        }

    def _lease_body(self) -> dict[str, Any]:
        ctx = self._context
        body = self._subject()
        body["lease_owner"] = ctx.lease_owner
        body["lease_version"] = ctx.lease_version
        return body

    # ── BootstrapClient ──────────────────────────────────────────────────

    async def claim(
        self,
        *,
        bootstrap_token: str,
        pod_uid: str,
        attempt_id: str,
        generation: int,
    ) -> BootstrapGrant:
        data = await self._post(
            "/internal/v1/runtime/bootstrap",
            {"pod_uid": pod_uid, "attempt_id": attempt_id, "generation": generation},
            token=bootstrap_token,
        )
        return BootstrapGrant(
            runtime_token=str(data["runtime_token"]),
            lease_owner=str(data.get("lease_owner", "")),
            lease_version=int(data.get("lease_version", 0)),
            expires_at=str(data.get("expires_at", "")),
            tenant_id=str(data.get("tenant_id", "")),
            run_id=str(data.get("run_id", "")),
            execution_unit_id=str(data.get("execution_unit_id", "")),
        )

    # ── RuntimeControlClient ─────────────────────────────────────────────

    async def restore(self, context: RuntimeContext) -> RuntimeCheckpoint:
        self.bind(context)
        data = await self._post(
            "/internal/v1/runtime/restore",
            self._lease_body(),
            token=context.runtime_token,
        )
        return RuntimeCheckpoint(
            checkpoint_id=str(data.get("checkpoint_id", "")),
            checkpoint_state=str(data.get("checkpoint_state", "")),
            snapshot_state=data.get("snapshot_state"),
            workflow_cursor=dict(data.get("workflow_cursor") or {}),
            agent_state=dict(data.get("agent_state") or {}),
            agent_state_schema_version=data.get("agent_state_schema_version"),
        )

    async def heartbeat(self, context: RuntimeContext) -> RuntimeContext:
        self.bind(context)
        data = await self._post(
            "/internal/v1/runtime/heartbeat",
            self._lease_body(),
            token=context.runtime_token,
        )
        refreshed = _restore_http_context(data, context)
        self.bind(refreshed)
        return refreshed

    async def model_call(
        self,
        *,
        model: dict[str, Any],
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        options: dict[str, Any],
    ) -> dict[str, Any]:
        """Proxy a full LLM call through the Internal API model-call endpoint."""
        return await self._post(
            "/internal/v1/runtime/model-call",
            {
                **self._subject(),
                "model": model,
                "system_prompt": system_prompt,
                "messages": messages,
                "tools": tools,
                "options": options,
            },
            token=self._context.runtime_token,
        )

    async def commit_checkpoint(
        self,
        context: RuntimeContext,
        *,
        agent_state: dict[str, object],
        agent_state_schema_version: str,
    ) -> None:
        self.bind(context)
        body = self._lease_body()
        body["agent_state"] = agent_state
        body["agent_state_schema_version"] = agent_state_schema_version
        await self._post(
            "/internal/v1/runtime/checkpoints",
            body,
            token=context.runtime_token,
        )

    async def commit_final_checkpoint(
        self,
        context: RuntimeContext,
        *,
        summary: str,
        agent_state: dict[str, object] | None = None,
        agent_state_schema_version: str | None = None,
    ) -> None:
        self.bind(context)
        body = self._lease_body()
        body["summary"] = summary
        body["agent_state"] = agent_state or {}
        body["agent_state_schema_version"] = (
            agent_state_schema_version or "http-runtime/v0"
        )
        await self._post(
            "/internal/v1/runtime/checkpoints/final",
            body,
            token=context.runtime_token,
        )

    async def record_failure(
        self, context: RuntimeContext, *, reason_code: str, retryable: bool
    ) -> None:
        self.bind(context)
        body = self._subject()
        body["reason_code"] = reason_code
        body["retryable"] = retryable
        await self._post(
            "/internal/v1/runtime/failures",
            body,
            token=context.runtime_token,
        )

    # ── TransportClient (remote tools) ───────────────────────────────────

    async def request(self, op: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        if op == OP_READ_TOOL:
            return await self._post(
                "/internal/v1/runtime/tools/read",
                {
                    **self._subject(),
                    "tool_name": str(kwargs.get("tool_name", "")),
                    "arguments_ref": str(kwargs.get("arguments", {}).get("resource_ref", ""))
                    or str(kwargs.get("arguments_ref", "")),
                },
                token=self._context.runtime_token,
            )
        if op == OP_PUBLISH_ARTIFACT:
            return await self._post(
                "/internal/v1/runtime/artifacts",
                {
                    **self._subject(),
                    "workspace_path": str(kwargs.get("workspace_path", "")),
                    "logical_name": str(kwargs.get("logical_name", "")),
                    "classification": str(kwargs.get("classification", "general")),
                },
                token=self._context.runtime_token,
            )
        if op == OP_PROPOSE_ACTION:
            return await self._post(
                "/internal/v1/runtime/action-proposals",
                {
                    **self._subject(),
                    "action_ref": str(kwargs.get("action_ref", "")),
                    "canonical_payload_ref": str(kwargs.get("canonical_payload_ref", "")),
                },
                token=self._context.runtime_token,
            )
        raise RuntimeError(f"http runtime does not support op {op}")


# ---------------------------------------------------------------------------
# HttpStream — stream_fn proxying LLM calls over the Internal API
# ---------------------------------------------------------------------------


def _make_http_stream_fn(client: HttpRuntimeClient) -> StreamFn:
    """Build the ``StreamFn`` that proxies LLM calls through model-call.

    Sends the full message history + tool definitions to the Control Plane,
    which proxies to the real provider (same wire contract as the pipe stream);
    the non-streaming response is translated back into
    ``AsyncIterator[AssistantMessageEvent]`` via the shared response emitter.
    """

    async def http_stream_fn(
        model: Model,
        context: AgentContext,
        options: SimpleStreamOptions,
    ) -> StreamResult:
        queue: asyncio.Queue[AssistantMessageEvent | None] = asyncio.Queue()
        done = asyncio.Event()
        state: dict[str, Any] = {"final": None}

        partial = AssistantMessage(
            api=model.api,
            provider=model.provider,
            model=model.id,
        )

        async def events_iter() -> AsyncIterator[AssistantMessageEvent]:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item

        async def get_result() -> AssistantMessage:
            await done.wait()
            final = state["final"]
            if final is None:
                raise RuntimeError("No result available")
            return final

        async def _run() -> None:
            nonlocal partial
            try:
                serialized_messages = [
                    m.model_dump() if hasattr(m, "model_dump") else m
                    for m in context.messages
                ]
                serialized_tools = []
                for t in context.tools:
                    parameters = (
                        t.parameters.model_dump()
                        if hasattr(t, "parameters") and hasattr(t.parameters, "model_dump")
                        else {"type": "object", "properties": {}, "required": []}
                    )
                    serialized_tools.append(
                        {
                            "name": t.name,
                            "description": getattr(t, "description", ""),
                            "label": getattr(t, "label", ""),
                            "parameters": parameters,
                        }
                    )
                response = await client.model_call(
                    model=model.model_dump(),
                    system_prompt=context.system_prompt,
                    messages=serialized_messages,
                    tools=serialized_tools,
                    options={
                        "temperature": options.temperature,
                        "max_tokens": options.max_tokens,
                        "reasoning": options.reasoning,
                    },
                )
                queue.put_nowait(StreamStartEvent(partial=partial))
                _emit_response_events(response, partial, queue)
                state["final"] = partial
            except Exception as error:  # noqa: BLE001 - surfaced as StreamErrorEvent
                partial.stop_reason = "error"
                partial.error_message = str(error)
                queue.put_nowait(StreamErrorEvent(reason="error", error=partial))
                state["final"] = partial
            finally:
                done.set()
                queue.put_nowait(None)

        asyncio.create_task(_run())
        return {"events": events_iter(), "result": get_result}

    return http_stream_fn


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _main() -> int:
    from enterprise_agent_platform.platform.logging_json import (
        install_json_logs_if_enabled,
    )

    install_json_logs_if_enabled()
    attempt_id = os.environ.get("AGENT_PLATFORM_ATTEMPT_ID", "").strip()
    generation_raw = os.environ.get("AGENT_PLATFORM_GENERATION", "1").strip()
    base_url = os.environ.get("AGENT_PLATFORM_CONTROL_PLANE_URL", "").strip()
    if not attempt_id:
        logger.error("AGENT_PLATFORM_ATTEMPT_ID is required in HTTP runtime mode")
        return 77
    if not base_url:
        logger.error("AGENT_PLATFORM_CONTROL_PLANE_URL is required in HTTP runtime mode")
        return 77
    generation = int(generation_raw) if generation_raw.isdigit() else 1

    client = HttpRuntimeClient(base_url)
    try:
        runtime = AgentRuntime(
            client,
            client,
            identity_provider=_EnvIdentityProvider(),
        )
        runtime.set_tools(
            native_tools=create_native_tools(),
            remote_tools=create_remote_tools(client),
        )
        runtime.set_stream_fn(_make_http_stream_fn(client))
        return await runtime.run(attempt_id=attempt_id, generation=generation)
    except RuntimeError as error:
        logger.error("http runtime failure: %s", error)
        return 1
    finally:
        await client.close()


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] (http-runtime) %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())