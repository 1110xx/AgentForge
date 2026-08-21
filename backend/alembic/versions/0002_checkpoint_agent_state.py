"""Add pi-agent-core agent_state snapshot columns to checkout checkpoint.

Revision ID: 0002_checkpoint_agent_state
Revises: 0001_agent_platform

The Run's durable checkpoint now carries the serialized pi-agent-core Agent
state (``Agent.state.model_dump()``) so a follow-up / rerun Attempt can
rehydrate conversation history instead of starting from a blank Agent.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_checkpoint_agent_state"
down_revision = "0001_agent_platform"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checkpoint",
        sa.Column("agent_state", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "checkpoint",
        sa.Column(
            "agent_state_schema_version",
            sa.String(64),
            nullable=False,
            server_default=sa.text("'pi-agent-core/v1'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("checkpoint", "agent_state_schema_version")
    op.drop_column("checkpoint", "agent_state")


__all__ = ["branch_labels", "depends_on", "down_revision", "revision"]