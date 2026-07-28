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
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func, text

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


class AlertRuleType(enum.StrEnum):
    """Closed set of alert-rule kinds an ``AlertRule`` row can be (D-05).

    Per-type payload shapes live in ``AlertRule.config`` (a JSON blob), not
    in separate tables — a ``NEW_FILING`` rule's config is ``{}`` (D-12), a
    ``PRICE_MOVE`` rule's is ``{"threshold_pct": <float>, "direction": "up"
    | "down" | "either"}`` (D-09), and a ``SCHEDULED`` rule's is
    ``{"cadence": "daily" | "weekly" | "monthly"}`` (D-11).
    """

    NEW_FILING = "NEW_FILING"
    PRICE_MOVE = "PRICE_MOVE"
    SCHEDULED = "SCHEDULED"


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


class FinancialMetric(Base):
    """Structured quarterly financial figure for a company (METRIC-01).

    Keyed by the three-column business key ``(ticker, metric_name, period)``
    (D-06) — a row is re-upserted on every research run rather than being
    append-only, so ``updated_at`` records the most recent successful
    refresh. Stores quarterly figures only (D-03); ``metric_name`` holds the
    canonical identifiers owned by ``app/services/financial_metrics_source.py``
    (revenue, net_income, gross_margin, operating_margin, debt_to_equity,
    free_cash_flow).
    """

    __tablename__ = "financial_metrics"
    __table_args__ = (
        UniqueConstraint(
            "ticker",
            "metric_name",
            "period",
            name="uq_financial_metrics_ticker_metric_period",
        ),
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    ticker = Column(
        String(20),
        ForeignKey("companies.ticker", ondelete="CASCADE"),
        nullable=False,
    )
    metric_name = Column(String(50), nullable=False)
    period = Column(String(10), nullable=False)
    value = Column(Float, nullable=False)
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


class WatchlistEntry(Base):
    """A user's watchlisted ticker — the parent row every ``AlertRule`` hangs off.

    ``(user_id, ticker)`` is unique (D-04): a watchlist is a set of tickers,
    not a multiset, so a repeated add-ticker request can only be a no-op. The
    row's ``Company`` parent is upserted on first use by
    ``app/services/company_service.py::ensure_company_exists`` (D-01) — any
    ticker string is accepted, not just ones Vantage has already researched.
    Both FKs cascade (D-03): removing a user or a company removes every
    watchlist entry that depended on it, and removing an entry removes its
    ``AlertRule`` rows in turn. No ``updated_at`` — a watchlist row is
    immutable once written (added or removed, never edited; D-08 rules out a
    watchlist-level enable/disable toggle, so there is nothing on this row to
    mutate in place).
    """

    __tablename__ = "watchlist_entries"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "ticker",
            name="uq_watchlist_entries_user_id_ticker",
        ),
    )

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
    ticker = Column(
        String(20),
        ForeignKey("companies.ticker", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class AlertRule(Base):
    """A configured alert on a watchlisted ticker (D-05: one table, JSON config).

    ``rule_type`` is a closed ``AlertRuleType`` enum; per-type payload shapes
    live in ``config`` (a JSON blob) rather than in three separate typed
    tables. Deliberately no ``UniqueConstraint`` on ``(watchlist_id,
    rule_type)`` — D-06 explicitly allows several rules of the same type on
    one entry (e.g. a 5% "heads up" and a 15% "urgent" ``PRICE_MOVE`` rule on
    the same ticker). There is no ``user_id`` column here: ownership is
    enforced entirely through the parent ``WatchlistEntry.user_id`` FK,
    mirroring how ``ChatMessage``'s docstring describes ownership flowing
    through its own parent memo. This phase never hard-deletes a rule (D-07)
    — the only deletion path is the D-03 cascade from its parent
    ``WatchlistEntry``. ``enabled`` defaults to ``True`` so a brand-new rule
    is live immediately (WATCH-08); ``updated_at`` records when that flag was
    last toggled.
    """

    __tablename__ = "alert_rules"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    watchlist_id = Column(
        UUID(as_uuid=True),
        ForeignKey("watchlist_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
    rule_type = Column(SAEnum(AlertRuleType), nullable=False)
    config = Column(JSON, nullable=False)
    enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
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


class ChatMessage(Base):
    """One turn (user question or assistant answer) in a memo's follow-up chat.

    Chat is strictly 1:1 with its ``ResearchMemo`` (D-01) — there is no
    separate ``ChatSession`` table; ``memo_id`` *is* the session identity.
    Ownership is enforced via the memo's own ``user_id`` FK, following the
    same 404-on-not-found-or-not-owned pattern used elsewhere (T-06-07-IDOR).
    ``coverage_exceeded`` is stored per assistant row so the CHAT-04 signal
    survives a refetch. No ``updated_at`` — a chat turn is immutable once
    written (mirrors ``AgentOutput``'s created_at-only shape).
    """

    __tablename__ = "chat_messages"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    memo_id = Column(
        UUID(as_uuid=True),
        ForeignKey("research_memos.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role = Column(String(20), nullable=False)  # "user" | "assistant"
    content = Column(Text, nullable=False)
    coverage_exceeded = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
