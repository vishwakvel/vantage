"""Add alert_events table and alert_rules.state column (Phase 11, D-03/D-06).

Revision ID: 005
Revises: 004
Create Date: 2026-07-30

alert_events is one new table serving BOTH WATCH-06 (in-app notifications)
and WATCH-07 (per-ticker alert history) — one row per alert trigger (D-06).
alert_rules.state is one new nullable JSON column holding evaluator-owned
per-rule runtime state (D-03): last_price/last_checked_at for PRICE_MOVE,
last_seen_accession for NEW_FILING, and last_triggered_at for SCHEDULED.
Both changes are purely additive. This revision never edits
001_initial_schema.py, 002_add_chat_messages.py, 003_add_financial_metrics.py,
or 004_add_watchlist_alert_rules.py, all of which are already applied to
running databases. Bundling both changes into one revision is deliberate
(11-CONTEXT.md Claude's Discretion) — they ship together and are
meaningless apart.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers used by Alembic
revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("alert_rule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column(
            "triggered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "read",
            sa.Boolean,
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["alert_rule_id"], ["alert_rules.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_alert_events_alert_rule_id", "alert_events", ["alert_rule_id"])

    op.add_column("alert_rules", sa.Column("state", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("alert_rules", "state")
    op.drop_index("ix_alert_events_alert_rule_id", table_name="alert_events")
    op.drop_table("alert_events")
