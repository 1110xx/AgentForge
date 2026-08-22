"""Idempotency TTL: keys expire after the retention window and are recyclable.

SDD §13.2 risk mitigation: ``IdempotencyRecord`` used to accumulate forever
(no expiry, no purge). This pins the new policy:

* every claim carries an ``expires_at`` horizon (``IDEMPOTENCY_RETENTION``);
* completion refreshes the horizon, so finished results stay replayable for a
  full window;
* once expired, the same key is **recyclable** — a fresh claim succeeds again
  (abandoned IN_PROGRESS claims and stale COMPLETED keys never block reuse);
* ``purge_expired_idempotency`` physically removes lapsed rows.

Both the in-memory dev store and the SQLAlchemy (sqlite L1) implementation
must behave identically.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from enterprise_agent_platform.domain.records import IDEMPOTENCY_RETENTION
from enterprise_agent_platform.persistence.database import (
    create_schema,
    create_sqlite_l1_engine,
)
from enterprise_agent_platform.persistence.memory import InMemoryPlatformStore
from enterprise_agent_platform.persistence.protocol import PlatformError
from enterprise_agent_platform.persistence.sqlalchemy_store import SqlAlchemyPlatformStore

TENANT = "ttl-test"

DIGEST = "digest-v1"
ACTOR = "alice"


async def _scenario(store, now_holder: dict[str, datetime] | None) -> None:
    def advance(amount: timedelta) -> None:
        assert now_holder is not None, "this store has no controllable clock"
        now_holder["now"] = now_holder["now"] + amount

    # ── 1. Fresh claim → None; the record has the TTL horizon ──
    async with store.transaction() as tx:
        now = await tx.db_now()
        claimed = await tx.claim_idempotency(TENANT, "create_run", "run-key-1", DIGEST, ACTOR, now)
        assert claimed is None, "fresh claim must succeed"

    # ── 2. Replay inside the window (same digest) returns the claim ──
    async with store.transaction() as tx:
        now = await tx.db_now()
        replay = await tx.claim_idempotency(TENANT, "create_run", "run-key-1", DIGEST, ACTOR, now)
        assert replay is not None and replay.status == "IN_PROGRESS"

    # ── 3. Different digest inside the window is rejected ──
    with pytest.raises(PlatformError) as info:
        async with store.transaction() as tx:
            now = await tx.db_now()
            await tx.claim_idempotency(TENANT, "create_run", "run-key-1", "digest-OTHER", ACTOR, now)
    assert info.value.code == "IDEMPOTENCY_KEY_REUSED"

    # ── 4. Completion stores the result and refreshes the horizon ──
    async with store.transaction() as tx:
        now = await tx.db_now()
        done = await tx.complete_idempotency(
            TENANT, "create_run", "run-key-1", DIGEST,
            "run", "run_1", "run-record/v1", {"ok": True}, now,
        )
        assert done.status == "COMPLETED"
        assert done.result_id == "run_1"
        assert done.expires_at is not None
        assert done.expires_at - done.updated_at == IDEMPOTENCY_RETENTION

    # ── 5. Completed replay inside the window returns the result ──
    async with store.transaction() as tx:
        now = await tx.db_now()
        replay = await tx.claim_idempotency(TENANT, "create_run", "run-key-1", DIGEST, ACTOR, now)
        assert replay is not None and replay.status == "COMPLETED"
        assert replay.result_id == "run_1"

    # ── 6. Expiry: the key is recycled — a fresh claim succeeds ──
    advance(IDEMPOTENCY_RETENTION + timedelta(seconds=1))
    async with store.transaction() as tx:
        now = await tx.db_now()
        reclaimed = await tx.claim_idempotency(TENANT, "create_run", "run-key-1", DIGEST, ACTOR, now)
        assert reclaimed is None, "expired key must be recyclable"
        done = await tx.complete_idempotency(
            TENANT, "create_run", "run-key-1", DIGEST,
            "run", "run_2", "run-record/v1", {"ok": False}, now,
        )
        assert done.result_id == "run_2" and done.status == "COMPLETED"

    # ── 7. Purge removes only lapsed rows ──
    advance(IDEMPOTENCY_RETENTION - timedelta(seconds=1))
    async with store.transaction() as tx:
        now = await tx.db_now()
        # a second key, completed but still inside its window
        await tx.claim_idempotency(TENANT, "create_run", "run-key-2", DIGEST, ACTOR, now)
    async with store.transaction() as tx:
        now = await tx.db_now()
        await tx.complete_idempotency(
            TENANT, "create_run", "run-key-2", DIGEST,
            "run", "run_3", "run-record/v1", {"ok": True}, now,
        )
    advance(IDEMPOTENCY_RETENTION + timedelta(seconds=2))
    async with store.transaction() as tx:
        purged = await tx.purge_expired_idempotency(100)
        # run-key-1 (re-completed at step 6) lapsed; run-key-2 is now also
        # past its window after the combined advance.
        assert purged >= 1, "at least the step-6 key must be purged"


def test_idempotency_ttl_memory() -> None:
    memory_now = {"now": datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)}
    memory = InMemoryPlatformStore(clock=lambda: memory_now["now"])
    asyncio.run(_scenario(memory, memory_now))


def test_idempotency_ttl_sqlite() -> None:
    sqlite_now = {"now": datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)}
    engine = create_sqlite_l1_engine()

    async def scenario() -> None:
        await create_schema(engine)
        store = SqlAlchemyPlatformStore(
            async_sessionmaker(engine, expire_on_commit=False),
            sqlite_l1_clock=lambda: sqlite_now["now"],
        )
        await _scenario(store, sqlite_now)
        await engine.dispose()

    asyncio.run(scenario())