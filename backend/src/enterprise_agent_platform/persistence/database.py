"""Async SQLAlchemy engine lifecycle for the standalone platform."""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from enterprise_agent_platform.config import PlatformSettings

from .tables import metadata


def create_platform_engine(
    settings: PlatformSettings,
    *,
    pool_size: int = 10,
    max_overflow: int = 10,
    pool_timeout: float = 30.0,
) -> AsyncEngine:
    """Create the production async engine without exposing the configured DSN."""
    return create_async_engine(
        settings.database_dsn(),
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
    )


def create_sqlite_l1_engine() -> AsyncEngine:
    """Create a single-connection in-memory SQLite engine for L1 adapter tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)


async def drop_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
