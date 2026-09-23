"""Add auditable strategy plans and append-only lifecycle events.

Revision ID: aa91f2283c14
Revises: 794490950747
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import tin.db

revision: str = "aa91f2283c14"
down_revision: Union[str, Sequence[str], None] = "794490950747"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("variety", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("plan", tin.db.JSONType, nullable=False),
        sa.Column("author", sa.String(length=64), nullable=False),
        sa.Column("revision_of", sa.Integer(), sa.ForeignKey("strategies.id"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", tin.db.UTCDateTime(), nullable=False),
        sa.Column("published_at", tin.db.UTCDateTime(), nullable=True),
        sa.Column("opened_at", tin.db.UTCDateTime(), nullable=True),
        sa.Column("closed_at", tin.db.UTCDateTime(), nullable=True),
        sa.Column("entry_fills", tin.db.JSONType, nullable=False),
        sa.Column("exit_fills", tin.db.JSONType, nullable=False),
    )
    op.create_index("ix_strategies_variety", "strategies", ["variety"])
    op.create_index("ix_strategies_status", "strategies", ["status"])
    op.create_table(
        "strategy_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("at", tin.db.UTCDateTime(), nullable=False),
        sa.Column("payload", tin.db.JSONType, nullable=False),
    )
    op.create_index("ix_strategy_events_strategy_id", "strategy_events", ["strategy_id"])


def downgrade() -> None:
    op.drop_index("ix_strategy_events_strategy_id", table_name="strategy_events")
    op.drop_table("strategy_events")
    op.drop_index("ix_strategies_status", table_name="strategies")
    op.drop_index("ix_strategies_variety", table_name="strategies")
    op.drop_table("strategies")
