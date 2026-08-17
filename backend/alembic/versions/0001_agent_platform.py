"""Create the standalone enterprise Agent platform frozen V1 schema.

Revision ID: 0001_agent_platform
Revises: None

Intentionally independent from live application metadata so later migrations
cannot silently rewrite the initial revision. The schema mirrors the frozen
SQLAlchemy Core metadata in enterprise_agent_platform.persistence.tables.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_agent_platform"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from enterprise_agent_platform.persistence.tables import metadata

    bind = op.get_bind()
    metadata.create_all(bind)


def downgrade() -> None:
    from enterprise_agent_platform.persistence.tables import metadata

    bind = op.get_bind()
    # Drop in reverse dependency order; circular FKs are deferred via SQLAlchemy.
    for table in reversed(metadata.sorted_tables):
        op.execute(sa.text(f'DROP TABLE IF EXISTS "{table.name}" CASCADE'))


__all__ = ["revision", "down_revision", "branch_labels", "depends_on"]
