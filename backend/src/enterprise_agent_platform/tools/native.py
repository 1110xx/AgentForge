"""Native local tool executors for pi-agent-core integration.

These tools execute directly in the Runner's local filesystem (``/tmp/workspace``)
without any Control Plane involvement. They are registered as ``AgentTool``
instances on the pi-agent-core ``Agent``.

Supported tools:
- ``file_read``  — read a file from the workspace
- ``file_write`` — write content to a file in the workspace
- ``bash``       — execute a shell command inside the workspace
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from pathlib import Path
from typing import Any

from pi_agent_core.types import (
    AgentTool,
    AgentToolResult,
    AgentToolSchema,
    AgentToolUpdateCallback,
    TextContent,
)

log = logging.getLogger(__name__)

_DEFAULT_WORKSPACE = Path("/tmp/workspace")


def _resolve_path(workspace: Path, path_str: str) -> Path:
    """Resolve a path relative to workspace, preventing directory traversal."""
    full = (workspace / path_str).resolve()
    if not str(full).startswith(str(workspace.resolve())):
        raise ValueError(f"path {path_str} escapes workspace boundary")
    return full


async def _execute_file_read(
    tool_call_id: str,
    args: dict[str, Any],
    cancel_event: asyncio.Event | None,
    on_update: AgentToolUpdateCallback | None,
    workspace: Path = _DEFAULT_WORKSPACE,
) -> AgentToolResult:
    """Read a file from the workspace."""
    path_str = args.get("path", "")
    if not path_str:
        return AgentToolResult(
            content=[TextContent(text="Error: 'path' argument is required")],
        )

    path = _resolve_path(workspace, path_str)
    try:
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return AgentToolResult(content=[TextContent(text=content)])
    except FileNotFoundError:
        return AgentToolResult(
            content=[TextContent(text=f"Error: file not found: {path_str}")],
            details={"is_error": True},
        )
    except Exception as exc:
        return AgentToolResult(
            content=[TextContent(text=f"Error reading file: {exc}")],
            details={"is_error": True},
        )


async def _execute_file_write(
    tool_call_id: str,
    args: dict[str, Any],
    cancel_event: asyncio.Event | None,
    on_update: AgentToolUpdateCallback | None,
    workspace: Path = _DEFAULT_WORKSPACE,
) -> AgentToolResult:
    """Write content to a file in the workspace."""
    path_str = args.get("path", "")
    content = args.get("content", "")
    if not path_str:
        return AgentToolResult(
            content=[TextContent(text="Error: 'path' argument is required")],
        )

    path = _resolve_path(workspace, path_str)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, content, encoding="utf-8")
        return AgentToolResult(
            content=[TextContent(text=f"Successfully wrote {len(content)} bytes to {path_str}")],
        )
    except Exception as exc:
        return AgentToolResult(
            content=[TextContent(text=f"Error writing file: {exc}")],
            details={"is_error": True},
        )


async def _execute_bash(
    tool_call_id: str,
    args: dict[str, Any],
    cancel_event: asyncio.Event | None,
    on_update: AgentToolUpdateCallback | None,
    workspace: Path = _DEFAULT_WORKSPACE,
) -> AgentToolResult:
    """Execute a shell command in the workspace directory."""
    command = args.get("command", "")
    if not command:
        return AgentToolResult(
            content=[TextContent(text="Error: 'command' argument is required")],
        )

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
            env={**os.environ, "WORKSPACE": str(workspace)},
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=30
        )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        output_parts = []
        if stdout:
            output_parts.append(stdout)
        if stderr:
            output_parts.append(f"[stderr]\n{stderr}")
        output = "\n".join(output_parts)

        return AgentToolResult(
            content=[TextContent(text=output or "(no output)")],
            details={"exit_code": proc.returncode, "stdout": stdout, "stderr": stderr},
        )
    except TimeoutError:
        return AgentToolResult(
            content=[TextContent(text="Error: command timed out after 30s")],
            details={"is_error": True},
        )
    except Exception as exc:
        return AgentToolResult(
            content=[TextContent(text=f"Error executing command: {exc}")],
            details={"is_error": True},
        )


def create_native_tools(workspace: str | Path | None = None) -> list[AgentTool]:
    """Create the list of native (local) AgentTool instances.

    Args:
        workspace: Path to the workspace directory. Defaults to ``/tmp/workspace``.

    Returns:
        A list of ``AgentTool`` objects for registration with pi-agent-core ``Agent.set_tools()``.
    """
    wsp = Path(workspace) if workspace else _DEFAULT_WORKSPACE

    file_read_tool = AgentTool(
        name="file_read",
        label="File Read",
        description="Read the content of a file from the workspace.",
        parameters=AgentToolSchema(
            properties={
                "path": {
                    "type": "string",
                    "description": "Path relative to the workspace root",
                },
            },
            required=["path"],
        ),
        execute=lambda id, args, cancel, on_update: _execute_file_read(
            id, args, cancel, on_update, workspace=wsp
        ),
    )

    file_write_tool = AgentTool(
        name="file_write",
        label="File Write",
        description="Write content to a file in the workspace. Creates parent directories if needed.",
        parameters=AgentToolSchema(
            properties={
                "path": {
                    "type": "string",
                    "description": "Path relative to the workspace root",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            required=["path", "content"],
        ),
        execute=lambda id, args, cancel, on_update: _execute_file_write(
            id, args, cancel, on_update, workspace=wsp
        ),
    )

    bash_tool = AgentTool(
        name="bash",
        label="Bash",
        description="Execute a shell command in the workspace directory.",
        parameters=AgentToolSchema(
            properties={
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
            },
            required=["command"],
        ),
        execute=lambda id, args, cancel, on_update: _execute_bash(
            id, args, cancel, on_update, workspace=wsp
        ),
    )

    return [file_read_tool, file_write_tool, bash_tool]