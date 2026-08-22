"""Portable process entrypoints with mandatory host-provided dependency factories."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import os
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

from fastapi import FastAPI

from enterprise_agent_platform.fastapi.app import create_agent_platform_app
from enterprise_agent_platform.fastapi.dependencies import AgentPlatformContainer


def _load_factory(variable: str) -> Callable[[], Any]:
    target = os.environ.get(variable, "").strip()
    if not target or ":" not in target:
        raise RuntimeError(f"{variable} must be an import path in module:callable form")
    module_name, attribute = target.split(":", 1)
    candidate = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(candidate):
        raise TypeError(f"{variable} does not resolve to a callable")
    return cast(Callable[[], Any], candidate)


def create_app_from_env() -> FastAPI:
    """Build the ASGI app without providing an unsafe fallback identity provider."""
    created = _load_factory("AGENT_PLATFORM_CONTAINER_FACTORY")()
    if isinstance(created, FastAPI):
        app = created
    elif isinstance(created, AgentPlatformContainer):
        app = create_agent_platform_app(created)
    else:
        raise TypeError("AGENT_PLATFORM_CONTAINER_FACTORY returned an unsupported object")

    # Newer Starlette versions include ``_IncludedRouter`` entries without a
    # ``path`` attribute; only real routes carry one.
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    if "/api/agent-platform/v1/health/live" not in paths:

        @app.get("/api/agent-platform/v1/health/live", include_in_schema=False)
        async def live() -> dict[str, str]:
            return {"status": "live"}

    if "/api/agent-platform/v1/health/ready" not in paths:

        @app.get("/api/agent-platform/v1/health/ready", include_in_schema=False)
        async def ready() -> dict[str, str]:
            return {"status": "ready"}

    return app


async def _run_worker() -> None:
    result = _load_factory("AGENT_PLATFORM_WORKER_FACTORY")()
    if not inspect.isawaitable(result):
        raise RuntimeError("AGENT_PLATFORM_WORKER_FACTORY must return an awaitable")
    await cast(Awaitable[object], result)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enterprise Agent Platform process")
    parser.add_argument("mode", choices=("api", "worker"))
    args = parser.parse_args(argv)
    if args.mode == "api":
        import uvicorn

        uvicorn.run(
            "enterprise_agent_platform.platform.entrypoint:create_app_from_env",
            factory=True,
            host="0.0.0.0",
            port=8080,
            proxy_headers=False,
            server_header=False,
        )
    else:
        asyncio.run(_run_worker())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
