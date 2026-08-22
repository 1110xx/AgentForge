"""Add an expiry (TTL) horizon to idempotency records.

Revision ID: 0003_idempotency_ttl
Revises: 0002_checkpoint_agent_state

The idempotency table used to retain claimed/completed keys forever
(SDD §13.2 risk: IdempotencyRecord accumulates permanently). ``expires_at``
carries the retention horizon: a key may be recycled once it lapses, and
expired rows can be purged by maintenance. ``None`` (legacy rows) is treated
as never-expired so existing records keep working until touched.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_idempotency_ttl"
down_revision = "0002_checkpoint_agent_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent guard (same pattern as 0002): any fresh database created from
    # the current Core metadata already carries ``expires_at`` via create_all,
    # so only databases that predate this migration get the ALTER.
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("idempotency_record")
    }
    if "expires_at" not in existing:
        op.add_column(
            "idempotency_record",
            sa.Column("expires_at", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    op.drop_column("idempotency_record", "expires_at")


__all__ = ["branch_labels", "depends_on", "down_revision", "revision"]