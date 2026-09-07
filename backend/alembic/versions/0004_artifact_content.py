"""Create the run-published artifact content table.

Revision ID: 0004_artifact_content
Revises: 0003_idempotency_ttl

SDD §13.4 M6-B: the agent's ``remote_publish_artifact`` now uploads the
workspace file bytes so the Control Plane can finalize the STAGING version to
READY (scan-clean) and serve the product for download. Content lives here,
keyed 1:1 with ``artifact_version`` (one immutable version → one blob).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_artifact_content"
down_revision = "0003_idempotency_ttl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from enterprise_agent_platform.persistence.tables import artifact_content_table

    artifact_content_table.create(bind=op.get_bind())


def downgrade() -> None:
    op.execute(sa.text('DROP TABLE IF EXISTS "artifact_content" CASCADE'))


__all__ = ["branch_labels", "depends_on", "down_revision", "revision"]
