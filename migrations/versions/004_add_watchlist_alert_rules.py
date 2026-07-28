"""Add watchlist_entries and alert_rules tables (Phase 10, D-03/D-04/D-05/D-06).

Revision ID: 004
Revises: 003
Create Date: 2026-07-28

watchlist_entries is a user's set of watched tickers, unique on
(user_id, ticker) (D-04). alert_rules is a single table with a rule_type
enum and a JSON config column, not three separate typed tables (D-05); it
declares no uniqueness on (watchlist_id, rule_type) because a ticker can
carry multiple rules of the same type (D-06). Both watchlist_entries' FKs
and alert_rules.watchlist_id cascade on delete (D-03), so removing a
watchlist entry removes its alert rules. This revision never edits
001_initial_schema.py, 002_add_chat_messages.py, or
003_add_financial_metrics.py, all three of which have already been applied
to running databases since Phases 1, 8, and 9 respectively.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers used by Alembic
revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watchlist_entries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ticker"], ["companies.ticker"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "user_id",
            "ticker",
            name="uq_watchlist_entries_user_id_ticker",
        ),
    )

    op.create_table(
        "alert_rules",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("watchlist_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "rule_type",
            sa.Enum("NEW_FILING", "PRICE_MOVE", "SCHEDULED", name="alertruletype"),
            nullable=False,
        ),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean,
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["watchlist_id"], ["watchlist_entries.id"], ondelete="CASCADE"
        ),
    )


def downgrade() -> None:
    op.drop_table("alert_rules")
    op.drop_table("watchlist_entries")
    sa.Enum(name="alertruletype").drop(op.get_bind(), checkfirst=True)
