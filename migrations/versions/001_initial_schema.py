"""Initial schema — creates all 9 Vantage v1.0 domain tables.

Revision ID: 001
Revises: (none — this is the first migration)
Create Date: 2026-06-27

Tables created in FK-safe order:
  1. users
  2. companies
  3. documents
  4. document_chunks
  5. research_requests
  6. research_plans
  7. research_memos
  8. agent_tasks
  9. agent_outputs

downgrade() drops tables in reverse FK-safe order.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers used by Alembic
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
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
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ------------------------------------------------------------------
    # 2. companies  (ticker is the PK — D-13)
    # ------------------------------------------------------------------
    op.create_table(
        "companies",
        sa.Column("ticker", sa.String(20), primary_key=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("sector", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ------------------------------------------------------------------
    # 3. documents
    # ------------------------------------------------------------------
    op.create_table(
        "documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("canonical_id", sa.String(255), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("ticker", sa.String(20), nullable=True),
        sa.Column(
            "source_type",
            sa.Enum(
                "EDGAR", "NEWS", "FRED", "ARXIV", "USER_UPLOAD",
                name="documentsourcetype",
            ),
            nullable=False,
        ),
        sa.Column(
            "visibility",
            sa.Enum("PUBLIC", "PRIVATE", name="documentvisibility"),
            nullable=False,
        ),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("url", sa.Text, nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["ticker"], ["companies.ticker"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("canonical_id", name="uq_documents_canonical_id"),
    )
    op.create_index(
        "ix_documents_canonical_id", "documents", ["canonical_id"], unique=True
    )

    # ------------------------------------------------------------------
    # 4. document_chunks
    # ------------------------------------------------------------------
    op.create_table(
        "document_chunks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(20), nullable=True),
        sa.Column("section", sa.String(255), nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding_id", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["ticker"], ["companies.ticker"], ondelete="SET NULL"
        ),
    )

    # ------------------------------------------------------------------
    # 5. research_requests
    # ------------------------------------------------------------------
    op.create_table(
        "research_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("raw_query", sa.Text, nullable=False),
        sa.Column("resolved_tickers", sa.JSON, nullable=True),
        sa.Column(
            "status",
            sa.String(50),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
    )

    # ------------------------------------------------------------------
    # 6. research_plans
    # ------------------------------------------------------------------
    op.create_table(
        "research_plans",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("resolved_tickers", sa.JSON, nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "INGESTION",
                "AGENT_EXECUTION",
                "SYNTHESIS",
                "COMPLETE",
                "FAILED",
                name="researchplanstatus",
            ),
            nullable=False,
        ),
        sa.Column("ingestion_status", sa.String(50), nullable=True),
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
            ["request_id"], ["research_requests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
    )

    # ------------------------------------------------------------------
    # 7. research_memos
    # ------------------------------------------------------------------
    op.create_table(
        "research_memos",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(20), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING", "RUNNING", "COMPLETE", "PARTIAL", "FAILED",
                name="researchmemostatus",
            ),
            nullable=False,
        ),
        sa.Column("body", sa.JSON, nullable=True),
        sa.Column(
            "parent_memo_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
            ["plan_id"], ["research_plans.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["ticker"], ["companies.ticker"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["parent_memo_id"],
            ["research_memos.id"],
            name="fk_research_memos_parent_memo_id",
        ),
    )

    # ------------------------------------------------------------------
    # 8. agent_tasks
    # ------------------------------------------------------------------
    op.create_table(
        "agent_tasks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("agent_type", sa.String(100), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING", "RUNNING", "SUCCESS", "PARTIAL", "FAILED",
                name="agenttaskstatus",
            ),
            nullable=False,
        ),
        sa.Column("input", sa.JSON, nullable=True),
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
            ["plan_id"], ["research_plans.id"], ondelete="CASCADE"
        ),
    )

    # ------------------------------------------------------------------
    # 9. agent_outputs
    # ------------------------------------------------------------------
    op.create_table(
        "agent_outputs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "completeness",
            sa.Enum("FULL", "PARTIAL", name="agentoutputcompleteness"),
            nullable=False,
        ),
        sa.Column("missing_fields", sa.JSON, nullable=True),
        sa.Column("output", sa.JSON, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["agent_tasks.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("task_id", name="uq_agent_outputs_task_id"),
    )


def downgrade() -> None:
    # Drop in reverse FK-safe order
    op.drop_table("agent_outputs")
    op.drop_table("agent_tasks")
    op.drop_table("research_memos")
    op.drop_table("research_plans")
    op.drop_table("research_requests")
    op.drop_table("document_chunks")
    op.drop_index("ix_documents_canonical_id", table_name="documents")
    op.drop_table("documents")
    op.drop_table("companies")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    # Drop PostgreSQL enum types created by upgrade()
    op.execute("DROP TYPE IF EXISTS agentoutputcompleteness")
    op.execute("DROP TYPE IF EXISTS agenttaskstatus")
    op.execute("DROP TYPE IF EXISTS researchmemostatus")
    op.execute("DROP TYPE IF EXISTS researchplanstatus")
    op.execute("DROP TYPE IF EXISTS documentvisibility")
    op.execute("DROP TYPE IF EXISTS documentsourcetype")
