#!/usr/bin/env python3
"""Smoke a clean wheel install using only documented public composition APIs."""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import re
from pathlib import Path

import enterprise_agent_platform as platform
from enterprise_agent_platform.reference.local_stack import create_app


async def _reference_vertical() -> None:
    harness = platform.ReferenceWorkflowHarness()
    paused = await harness.run_to_approval()
    completed = await harness.approve_and_complete(
        paused,
        actor_id="wheel-reviewer",
        client_action_id="wheel-approval-1",
    )
    assert completed.run.status.value == "SUCCEEDED"


def _assert_wheel_contents() -> None:
    distribution = importlib.metadata.distribution("enterprise-agent-platform")
    installed_roots = {
        str(path).replace("\\", "/").split("/", 1)[0] for path in distribution.files or ()
    }
    dist_info_roots = {
        item
        for item in installed_roots
        if re.fullmatch(r"enterprise_agent_platform-[A-Za-z0-9_.]+\.dist-info", item)
    }
    assert len(dist_info_roots) == 1, dist_info_roots
    unexpected = sorted(installed_roots - {"enterprise_agent_platform"} - dist_info_roots)
    assert not unexpected, f"unexpected top-level wheel content: {unexpected}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forbid-source", required=True, type=Path)
    args = parser.parse_args()
    loaded = Path(platform.__file__).resolve()
    forbidden = args.forbid_source.resolve()
    assert not loaded.is_relative_to(forbidden), (loaded, forbidden)
    assert {
        "ApprovalDecisionService",
        "CapabilityIssuer",
        "CapabilityVerifier",
        "DurableEffectExecutor",
        "EffectCapabilityAuthorizer",
        "EffectPayloadResolver",
        "PlatformStore",
        "EffectGrantRequest",
        "create_app",
        "create_in_memory_container",
        "create_router",
    } <= set(platform.__all__)
    create_app()
    _assert_wheel_contents()
    asyncio.run(_reference_vertical())
    print(f"wheel smoke passed: {loaded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
