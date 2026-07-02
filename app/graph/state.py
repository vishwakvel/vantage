"""Shared LangGraph state schema for the 2-node research graph.

``AgentGraphState`` carries every field the FundamentalAnalysis and
Synthesis nodes read or write (04-04-PLAN.md). The graph is compiled
WITHOUT a checkpointer (see ``app/graph/research_graph.py``), so this
state lives only in-memory for the lifetime of a single ``ainvoke`` call —
``session`` (the request-scoped ``AsyncSession``) is never serialized.
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
    """

    plan_id: str
    ticker: str
    user_id: str
    session: Any
    fundamentals_output: dict | None
    fundamentals_status: str
    synthesis_output: dict | None
    synthesis_status: str
    memo_status: str
