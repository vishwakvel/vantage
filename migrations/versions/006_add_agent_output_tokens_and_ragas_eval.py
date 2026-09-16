"""Add agent_outputs token columns and ragas_eval_results table (Phase 12, D-06/D-10).

Revision ID: 006
Revises: 005
Create Date: 2026-08-01

agent_outputs.prompt_tokens and agent_outputs.completion_tokens are two new
nullable Integer columns serving OBS-02 (per-agent token counts) — both must
be nullable because every agent has failure paths that write an AgentOutput
row without ever reaching a Groq call, and every row written before this
migration has no token data at all. ragas_eval_results is one new standalone
table serving OBS-03 (offline RAGAS eval scores). Both changes are bundled
into one revision because they ship together in this phase (12-02-PLAN.md).
ragas_eval_results carries no foreign key: the golden set is authored
independently of any user's research run and evaluates hybrid_retrieve in
the abstract, so there is no natural per-memo or per-user relationship to
model (D-10; see also the RagasEvalResult model docstring). This revision
never edits 001_initial_schema.py, 002_add_chat_messages.py,
003_add_financial_metrics.py, 004_add_watchlist_alert_rules.py, or
005_add_alert_events_and_rule_state.py, all of which are already applied to
running databases.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers used by Alembic
revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_outputs", sa.Column("prompt_tokens", sa.Integer(), nullable=True))
    op.add_column("agent_outputs", sa.Column("completion_tokens", sa.Integer(), nullable=True))

    op.create_table(
        "ragas_eval_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("case_id", sa.String(100), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("context_precision", sa.Float(), nullable=True),
        sa.Column("context_recall", sa.Float(), nullable=True),
        sa.Column("retrieved_count", sa.Integer(), nullable=True),
    )
    op.create_index("ix_ragas_eval_results_run_at", "ragas_eval_results", ["run_at"])


def downgrade() -> None:
    op.drop_index("ix_ragas_eval_results_run_at", table_name="ragas_eval_results")
    op.drop_table("ragas_eval_results")
    op.drop_column("agent_outputs", "completion_tokens")
    op.drop_column("agent_outputs", "prompt_tokens")
