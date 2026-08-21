"""Tool execution ports and agent-tool integration.

This module bridges two concerns:

1. **Runtime-side tool execution** (new — pi-agent-core integration)
   - ``native.py``: Local tool executors (file_read, file_write, bash) that
     run directly in the Runner's workspace.
   - ``remote.py``: Remote tool executors (remote_read_tool, etc.) that
     send requests through the Transport layer to the Control Plane.

2. **Control Plane-side tool governance** (existing — stub/reconstructed)
   - ``registry.py``: ``ToolRegistry``, ``ToolSpec``, ``ToolGrant`` — tool
     metadata and runtime permission grants.
   - ``connectors.py``: ``Connector`` Protocol — external system invocation
     boundary with idempotency at the effect/operation level.
   - ``gateway.py``: ``ToolGateway`` — re-verifies spec/grant/capability
     before invoking a Connector, records immutable results.
   - ``durable_effects.py``: ``DurableEffectExecutor`` — executes and
     reconciles Durable Effects through Connectors with full transactional
     lifecycle (PREPARED → EXECUTING → SUCCEEDED/FAILED/UNKNOWN).
"""

from enterprise_agent_platform.tools.native import create_native_tools
from enterprise_agent_platform.tools.remote import create_remote_tools

__all__ = [
    # Runtime-side tool creation
    "create_native_tools",
    "create_remote_tools",
]