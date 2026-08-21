"""Remote tool executors for pi-agent-core integration.

These tools execute by sending requests through the **Transport** layer to the
Control Plane, which proxies them to the appropriate platform services
(ResourceResolver, RunSessionProvider, persistence, approvals, etc.).

Supported tools:
- ``remote_read_tool``        — read a platform resource via Control Plane
- ``remote_publish_artifact`` — publish an artifact to MinIO via Control Plane
- ``remote_propose_action``   — propose an action for approval via Control Plane
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from pi_agent_core.types import (
    AgentTool,
    AgentToolResult,
    AgentToolSchema,
    AgentToolUpdateCallback,
    TextContent,
)

from enterprise_agent_platform.execution.pipe_transport import (
    OP_PUBLISH_ARTIFACT,
    OP_PROPOSE_ACTION,
    OP_READ_TOOL,
)

log = logging.getLogger(__name__)


class TransportClient(Protocol):
    """Protocol for sending a tool request through the transport layer."""

    async def request(self, op: str, kwargs: dict[str, Any]) -> dict[str, Any]: ...


async def _execute_remote_read_tool(
    tool_call_id: str,
    args: dict[str, Any],
    cancel_event: asyncio.Event | None,
    on_update: AgentToolUpdateCallback | None,
    transport: TransportClient,
) -> AgentToolResult:
    """Read a platform resource via the Control Plane."""
    tool_name = args.get("tool_name", "")
    tool_args = args.get("arguments", {})

    if not tool_name:
        return AgentToolResult(
            content=[TextContent(text="Error: 'tool_name' argument is required")],
        )

    try:
        response = await transport.request(
            OP_READ_TOOL,
            {
                "tool_name": tool_name,
                "arguments": tool_args,
            },
        )
        result = response.get("result", "")
        return AgentToolResult(content=[TextContent(text=str(result))])
    except Exception as exc:
        return AgentToolResult(
            content=[TextContent(text=f"Error reading tool via Control Plane: {exc}")],
            details={"is_error": True},
        )


async def _execute_remote_publish_artifact(
    tool_call_id: str,
    args: dict[str, Any],
    cancel_event: asyncio.Event | None,
    on_update: AgentToolUpdateCallback | None,
    transport: TransportClient,
) -> AgentToolResult:
    """Publish an artifact to MinIO via the Control Plane."""
    workspace_path = args.get("workspace_path", "")
    logical_name = args.get("logical_name", "")

    if not workspace_path:
        return AgentToolResult(
            content=[TextContent(text="Error: 'workspace_path' argument is required")],
        )

    try:
        response = await transport.request(
            OP_PUBLISH_ARTIFACT,
            {
                "workspace_path": workspace_path,
                "logical_name": logical_name,
                "classification": args.get("classification", "general"),
            },
        )
        status = response.get("status", "accepted")
        return AgentToolResult(
            content=[TextContent(text=f"Artifact publish {status}: {logical_name}")],
            details={"status": status, "logical_name": logical_name},
        )
    except Exception as exc:
        return AgentToolResult(
            content=[TextContent(text=f"Error publishing artifact: {exc}")],
            details={"is_error": True},
        )


async def _execute_remote_propose_action(
    tool_call_id: str,
    args: dict[str, Any],
    cancel_event: asyncio.Event | None,
    on_update: AgentToolUpdateCallback | None,
    transport: TransportClient,
) -> AgentToolResult:
    """Propose an action for approval via the Control Plane."""
    action_ref = args.get("action_ref", "")
    payload_ref = args.get("canonical_payload_ref", "")

    if not action_ref:
        return AgentToolResult(
            content=[TextContent(text="Error: 'action_ref' argument is required")],
        )

    try:
        response = await transport.request(
            OP_PROPOSE_ACTION,
            {
                "action_ref": action_ref,
                "canonical_payload_ref": payload_ref,
            },
        )
        status = response.get("status", "accepted")
        return AgentToolResult(
            content=[TextContent(text=f"Action proposal {status}: {action_ref}")],
            details={"status": status, "action_ref": action_ref},
        )
    except Exception as exc:
        return AgentToolResult(
            content=[TextContent(text=f"Error proposing action: {exc}")],
            details={"is_error": True},
        )


def create_remote_tools(transport: TransportClient) -> list[AgentTool]:
    """Create the list of remote (Control-Plane-proxied) AgentTool instances.

    Args:
        transport: A ``TransportClient`` implementation (e.g. ``PipeClient``
            for subprocess mode, or an HTTP client for Docker/K8s mode).

    Returns:
        A list of ``AgentTool`` objects for registration with pi-agent-core ``Agent.set_tools()``.
    """
    read_tool = AgentTool(
        name="remote_read_tool",
        label="Remote Read Tool",
        description=(
            "Read a platform resource through the Control Plane. "
            "Use this for resources outside the local workspace."
        ),
        parameters=AgentToolSchema(
            properties={
                "tool_name": {
                    "type": "string",
                    "description": "Name of the tool/resource to read",
                },
                "arguments": {
                    "type": "object",
                    "description": "Arguments for the tool (optional)",
                },
            },
            required=["tool_name"],
        ),
        execute=lambda id, args, cancel, on_update: _execute_remote_read_tool(
            id, args, cancel, on_update, transport=transport
        ),
    )

    publish_artifact_tool = AgentTool(
        name="remote_publish_artifact",
        label="Remote Publish Artifact",
        description="Publish an artifact to the platform artifact store via the Control Plane.",
        parameters=AgentToolSchema(
            properties={
                "workspace_path": {
                    "type": "string",
                    "description": "Path to the artifact file in the workspace",
                },
                "logical_name": {
                    "type": "string",
                    "description": "Logical name for the artifact",
                },
                "classification": {
                    "type": "string",
                    "description": "Artifact classification (e.g. 'report', 'data', 'image')",
                },
            },
            required=["workspace_path"],
        ),
        execute=lambda id, args, cancel, on_update: _execute_remote_publish_artifact(
            id, args, cancel, on_update, transport=transport
        ),
    )

    propose_action_tool = AgentTool(
        name="remote_propose_action",
        label="Remote Propose Action",
        description="Propose an action for approval through the Control Plane.",
        parameters=AgentToolSchema(
            properties={
                "action_ref": {
                    "type": "string",
                    "description": "Reference to the action definition",
                },
                "canonical_payload_ref": {
                    "type": "string",
                    "description": "Reference to the canonical payload (optional)",
                },
            },
            required=["action_ref"],
        ),
        execute=lambda id, args, cancel, on_update: _execute_remote_propose_action(
            id, args, cancel, on_update, transport=transport
        ),
    )

    return [read_tool, publish_artifact_tool, propose_action_tool]