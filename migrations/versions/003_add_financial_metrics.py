"""Add financial_metrics table — structured metric storage (Phase 9, D-03/D-06).

Revision ID: 003
Revises: 002
Create Date: 2026-07-27

financial_metrics is keyed by the three-column business key
(ticker, metric_name, period) (D-06) -- rows are upserted on every research
run, not append-only. Metrics are stored at quarterly granularity only
(D-03). This revision never edits 001_initial_schema.py or
002_add_chat_messages.py, both of which have already been applied to
running databases since Phases 1 and 8 respectively.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers used by Alembic
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_metrics",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("metric_name", sa.String(50), nullable=False),
        sa.Column("period", sa.String(10), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
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
        sa.ForeignKeyConstraint(["ticker"], ["companies.ticker"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "ticker",
            "metric_name",
            "period",
            name="uq_financial_metrics_ticker_metric_period",
        ),
    )


def downgrade() -> None:
    op.drop_table("financial_metrics")
