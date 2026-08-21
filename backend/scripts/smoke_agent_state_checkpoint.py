"""Smoke test: CheckpointRecord.agent_state roundtrip through both stores.

Verifies the Phase-1 data layer change:
  create_run -> initial checkpoint (agent_state={})
  reserve_attempt -> activate_lease -> commit_checkpoint(agent_state payload)
  get_checkpoint -> agent_state read back intact (dict deep-equality)

Runs against InMemoryPlatformStore and SqlAlchemyPlatformStore (SQLite L1).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from enterprise_agent_platform.contracts.commands import CreateRunCommand
from enterprise_agent_platform.control.checkpoints import CheckpointCommit, commit_checkpoint
from enterprise_agent_platform.control.context import RequestContext
from enterprise_agent_platform.control.service import ControlPlaneService
from enterprise_agent_platform.persistence.protocol import PlatformStore
from enterprise_agent_platform.reference.local_stack import REFERENCE_LOCAL_TENANT, create_container

AGENT_STATE = {
    "system_prompt": "You are a smoke tester.",
    "thinking_level": "off",
    "model": {"api": "deepseek", "provider": "deepseek", "id": "deepseek-chat"},
    "tools": [
        {
            "name": "file_read",
            "description": "read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "first question"}]},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "first answer"}],
            "stop_reason": "end_turn",
            "usage": {"input": 10, "output": 20, "total_tokens": 30},
        },
    ],
}


def _ctx() -> RequestContext:
    return RequestContext(
        tenant_id=REFERENCE_LOCAL_TENANT,
        actor_id="smoke-actor",
        scopes=(),
        request_id="smoke-request",
        trace_id="smoke-trace",
    )


async def _lifecycle(store: PlatformStore) -> None:
    control = ControlPlaneService(store)
    ctx = _ctx()
    run = await control.create_run(
        ctx,
        CreateRunCommand(
            workflow_type="synthetic-analysis",
            intent="Analyze smoke",
            resource_refs=("synthetic-case:case-42",),
            parameters={"analysis_mode": "summary", "max_items": 10},
            host_context_ref="reference-context:demo",
        ),
        idempotency_key="smoke-create-run",
    )
    unit = await store.get_primary_unit(ctx.tenant_id, run.run_id)
    initial_ckpt = await store.get_checkpoint(ctx.tenant_id, unit.current_checkpoint_id)
    assert initial_ckpt.agent_state == {}
    assert initial_ckpt.agent_state_schema_version == "pi-agent-core/v1"

    reservation = await control.reserve_attempt(
        ctx,
        execution_unit_id=unit.execution_unit_id,
        source_checkpoint_id=unit.current_checkpoint_id,
        expected_unit_version=unit.version,
        transition_key="smoke-reserve",
    )
    lease = await control.activate_lease(
        ctx,
        reservation.attempt.attempt_id,
        reservation.attempt.generation,
        owner="smoke-owner",
        expected_lease_version=1,
    )

    # Simulate the Runner: CLAIMED -> RUNNING -> CHECKPOINTING (CAS against store,
    # mirroring what reference/provider.py does before pause_for_approval).
    from enterprise_agent_platform.contracts.enums import AttemptState, EntityType
    from enterprise_agent_platform.domain.fsm import transition
    from dataclasses import replace as _replace

    attempt = await store.get_attempt(ctx.tenant_id, reservation.attempt.attempt_id)
    transition(EntityType.ATTEMPT, attempt.status, AttemptState.RUNNING, None)
    async with store.transaction() as tx:
        now = await tx.db_now()
        running = _replace(attempt, status=AttemptState.RUNNING, version=attempt.version + 1, updated_at=now)
        await tx.replace_attempt_cas(running, attempt.version)
        checkpointing = _replace(running, status=AttemptState.CHECKPOINTING, version=running.version + 1, updated_at=now)
        await tx.replace_attempt_cas(checkpointing, running.version)

    checkpoint = await commit_checkpoint(
        store,
        ctx,
        attempt_id=reservation.attempt.attempt_id,
        generation=reservation.attempt.generation,
        lease_owner="smoke-owner",
        expected_lease_version=lease.version,
        command=CheckpointCommit(
            source_checkpoint_id=unit.current_checkpoint_id,
            workflow_cursor={"intent": "Analyze smoke", "resource_refs": ["synthetic-case:case-42"]},
            checksum="smoke-checksum",
            agent_state=AGENT_STATE,
            agent_state_schema_version="pi-agent-core/v1",
        ),
    )
    assert checkpoint.checkpoint_seq == 1
    assert checkpoint.agent_state == AGENT_STATE

    read_back = await store.get_checkpoint(ctx.tenant_id, checkpoint.checkpoint_id)
    assert read_back.agent_state == AGENT_STATE, "agent_state roundtrip mismatch"
    assert read_back.agent_state_schema_version == "pi-agent-core/v1"
    print(
        f"  OK checkpoint_seq={read_back.checkpoint_seq} "
        f"agent_state_keys={sorted(read_back.agent_state)}"
    )


async def _in_memory() -> None:
    container = create_container()
    print("[InMemoryPlatformStore] running lifecycle...")
    await _lifecycle(container.store)


async def _sqlalchemy() -> None:
    from enterprise_agent_platform.persistence.database import (
        create_schema,
        create_sqlite_l1_engine,
        drop_schema,
    )
    from enterprise_agent_platform.persistence.sqlalchemy_store import SqlAlchemyPlatformStore
    from sqlalchemy.ext.asyncio import async_sessionmaker

    class _Clock:
        def __init__(self) -> None:
            self._now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)

        def __call__(self) -> datetime:
            self._now = self._now + timedelta(seconds=1)
            return self._now

    engine = create_sqlite_l1_engine()
    await create_schema(engine)
    try:
        import uuid as _uuid

        store = SqlAlchemyPlatformStore(
            async_sessionmaker(engine, expire_on_commit=False),
            id_factory=lambda kind: f"{kind}_{_uuid.uuid4().hex[:12]}",
            sqlite_l1_clock=_Clock(),
        )
        print("[SqlAlchemyPlatformStore] running lifecycle...")
        await _lifecycle(store)
    finally:
        from sqlalchemy import text

        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA foreign_keys=OFF"))
        await drop_schema(engine)
        await engine.dispose()


async def main() -> None:
    await _in_memory()
    await _sqlalchemy()
    print("ALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())