"""Shared LangGraph state schema for the 5-specialist + Synthesis fan-in
research graph.

``AgentGraphState`` carries every field the 5 specialist agents
(FundamentalAnalysis, SentimentNLP, RiskAssessment, MacroSector,
ComparableCompanies) and the Synthesis fan-in node read or write. The graph
is compiled WITHOUT a checkpointer (see ``app/graph/research_graph.py``), so
this state lives only in-memory for the lifetime of a single ``ainvoke``
call — ``session`` (the request-scoped ``AsyncSession``) is never
serialized.
"""

from __future__ import annotations

from typing import Any, TypedDict


class AgentGraphState(TypedDict):
    """In-memory state passed between graph nodes for one research run.

    ``session`` is typed ``Any`` deliberately — it carries the request-scoped
    ``AsyncSession`` without importing the session type into the graph
    module (keeps ``app/graph`` free of DB-layer coupling). Because the
    graph is compiled without a checkpointer, no attempt is ever made to
    serialize ``session`` (or any other field) to persistent storage.

    ``memo_id`` identifies the ``ResearchMemo`` row this run writes to and
    selects the per-memo Redis progress channel
    (``app.services.progress_publisher.progress_channel``) that
    ``_with_progress`` (``app/graph/research_graph.py``) publishes
    per-agent status transitions on (EXEC-01). Callers that build state
    without ``memo_id`` (e.g. existing graph integration tests) remain
    valid — ``_with_progress`` treats a missing/empty ``memo_id`` as
    "no progress emit" and no-ops.
    """

    plan_id: str
    memo_id: str
    ticker: str
    user_id: str
    session: Any
    fundamentals_output: dict | None
    fundamentals_status: str
    sentiment_output: dict | None
    sentiment_status: str
    risk_output: dict | None
    risk_status: str
    macro_output: dict | None
    macro_status: str
    comparables_output: dict | None
    comparables_status: str
    synthesis_output: dict | None
    synthesis_status: str
    memo_status: str
