"""SQLAlchemy ORM models for the Vantage v1.0 schema.

All 9 domain tables are defined here so that a single import populates
``Base.metadata`` for Alembic autogenerate::

    from app.db.base import Base
    from app.db import models  # registers all tables via side-effect
    target_metadata = Base.metadata

Primary key conventions (D-12, D-13):
- All tables except ``companies`` use UUID PKs (server-generated via PostgreSQL
  ``gen_random_uuid()``).
- ``companies.ticker`` is a VARCHAR(20) PRIMARY KEY — the ticker IS the PK.
  Upper-case normalisation is enforced at the service layer (not here).

Status columns use Python ``str + Enum`` combined with SQLAlchemy's
``Enum`` type so the DB stores a VARCHAR with a CHECK constraint and Python
gets full enum semantics without requiring ``ALTER TYPE`` for new values (D-14).
"""

from __future__ import annotations

import enum

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.base import Base

# ---------------------------------------------------------------------------
# Status / type enums
# ---------------------------------------------------------------------------


class ResearchMemoStatus(enum.StrEnum):
    """Lifecycle states for a ResearchMemo."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class ResearchPlanStatus(enum.StrEnum):
    """Lifecycle states for a ResearchPlan (mirrors execution phases)."""

    PENDING = "PENDING"
    INGESTION = "INGESTION"
    AGENT_EXECUTION = "AGENT_EXECUTION"
    SYNTHESIS = "SYNTHESIS"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class AgentTaskStatus(enum.StrEnum):
    """Lifecycle states for an AgentTask."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class DocumentVisibility(enum.StrEnum):
    """Visibility scope for a Document."""

    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"


class DocumentSourceType(enum.StrEnum):
    """Origin system of a Document."""

    EDGAR = "EDGAR"
    NEWS = "NEWS"
    FRED = "FRED"
    ARXIV = "ARXIV"
    USER_UPLOAD = "USER_UPLOAD"


class AgentOutputCompleteness(enum.StrEnum):
    """Whether an AgentOutput was fully or partially populated."""

    FULL = "FULL"
    PARTIAL = "PARTIAL"


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------


class User(Base):
    """Platform tenant — owns ResearchMemos, private Documents, and Sessions."""

    __tablename__ = "users"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Company(Base):
    """Canonical company record keyed by ticker symbol.

    ``ticker`` is the PRIMARY KEY (VARCHAR 20, uppercase enforced at service
    layer — D-13).  No UUID PK on this table.
    """

    __tablename__ = "companies"

    ticker = Column(String(20), primary_key=True)
    name = Column(String(255), nullable=True)
    sector = Column(String(100), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class Document(Base):
    """Canonical ingestion unit — one filing, article, PDF, or data series snapshot.

    ``canonical_id`` is a deterministic hash for deduplication across sources.
    Public documents (EDGAR, news, FRED) are global; private documents are
    user-scoped (``user_id`` non-null, ``visibility=PRIVATE``).
    """

    __tablename__ = "documents"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    canonical_id = Column(String(255), unique=True, nullable=False, index=True)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    ticker = Column(
        String(20),
        ForeignKey("companies.ticker", ondelete="SET NULL"),
        nullable=True,
    )
    source_type = Column(SAEnum(DocumentSourceType), nullable=False)
    visibility = Column(
        SAEnum(DocumentVisibility),
        nullable=False,
        default=DocumentVisibility.PUBLIC,
    )
    title = Column(String(512), nullable=True)
    url = Column(Text, nullable=True)
    fetched_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class DocumentChunk(Base):
    """Sub-unit of a Document produced during ingestion — the unit of retrieval.

    ``embedding_id`` references the vector in ChromaDB.
    ``section`` is a plain string enforced by convention (see section_constants.py).
    """

    __tablename__ = "document_chunks"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    ticker = Column(
        String(20),
        ForeignKey("companies.ticker", ondelete="SET NULL"),
        nullable=True,
    )
    section = Column(String(255), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    embedding_id = Column(String(255), nullable=True)  # ChromaDB vector reference
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ResearchRequest(Base):
    """Raw user input to the system.

    ``resolved_tickers`` is a JSON list of ticker strings populated after
    disambiguation by the Orchestrator.
    """

    __tablename__ = "research_requests"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    raw_query = Column(Text, nullable=False)
    resolved_tickers = Column(JSON, nullable=True)
    status = Column(String(50), nullable=False, default="PENDING")
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ResearchPlan(Base):
    """Orchestrator execution plan derived from a ResearchRequest.

    Tracks which agents will run, their inputs, and execution phases.
    Has two independent status fields: ``status`` and ``ingestion_status``.
    """

    __tablename__ = "research_plans"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    request_id = Column(
        UUID(as_uuid=True),
        ForeignKey("research_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    resolved_tickers = Column(JSON, nullable=True)
    status = Column(
        SAEnum(ResearchPlanStatus),
        nullable=False,
        default=ResearchPlanStatus.PENDING,
    )
    ingestion_status = Column(String(50), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ResearchMemo(Base):
    """Primary output artifact — structured, cited investment research document.

    Soft-deleted only (``deleted_at`` non-null means deleted); never hard-deleted.
    ``parent_memo_id`` links to a prior memo for lineage tracking.
    ``body`` stores the full structured memo as JSON.
    """

    __tablename__ = "research_memos"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    plan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("research_plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    ticker = Column(
        String(20),
        ForeignKey("companies.ticker", ondelete="SET NULL"),
        nullable=True,
    )
    status = Column(
        SAEnum(ResearchMemoStatus),
        nullable=False,
        default=ResearchMemoStatus.PENDING,
    )
    body = Column(JSON, nullable=True)
    parent_memo_id = Column(
        UUID(as_uuid=True),
        ForeignKey("research_memos.id"),  # self-referential; no cascade
        nullable=True,
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)  # soft delete
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AgentTask(Base):
    """Unit of work delegated to a specialist agent within a ResearchPlan.

    ``input`` stores the typed input dict for the agent (serialised JSON).
    """

    __tablename__ = "agent_tasks"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    plan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("research_plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_type = Column(String(100), nullable=False)
    status = Column(
        SAEnum(AgentTaskStatus),
        nullable=False,
        default=AgentTaskStatus.PENDING,
    )
    input = Column(JSON, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AgentOutput(Base):
    """Typed, structured result of an AgentTask.

    One output per task (UNIQUE on ``task_id``).
    ``missing_fields`` is a JSON list of field names not populated (PARTIAL only).
    ``output`` stores the full agent output as JSON (required, never null).
    """

    __tablename__ = "agent_outputs"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # one output per task
    )
    completeness = Column(SAEnum(AgentOutputCompleteness), nullable=False)
    missing_fields = Column(JSON, nullable=True)
    output = Column(JSON, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
