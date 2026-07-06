"""Celery task running the research graph asynchronously (EXEC-05).

This is the lift-and-shift of the research execution body that used to run
inline in ``POST /research/{plan_id}/run`` (see ``app/api/v1/research.py``
lines 587-654 prior to this plan). The PENDING ``ResearchMemo`` row is
created synchronously by the endpoint (06-05); this task picks it up by
``memo_id``, runs the graph, assembles the full six-section body (EXEC-04
reasons intact), updates the SAME memo row to its terminal status, and
publishes a terminal progress event (D-10) so the WebSocket route
(``app/api/v1/ws.py``, 06-04) can close.

``_AGENT_TYPE_BY_SECTION``, ``_SECTION_STATE_FIELDS``, and ``_extract_reason``
are moved here verbatim from ``app/api/v1/research.py`` — they will be
deleted from that module in 06-05; this module is the new source of truth.

Event-loop safety: each task invocation resets every module-level singleton
that wraps a persistent async network client (DB engine, and the httpx-based
EDGAR/news/arXiv/Groq clients) before its own ``asyncio.run(...)``, so none
of them are reused from a prior (now-closed) task's event loop — reusing an
asyncpg connection or httpx.AsyncClient bound to a closed loop raises
"RuntimeError: Event loop is closed" (see
``app/db/session.py::reset_session_factory`` docstring, which the other
four resets mirror exactly).
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from app.db.models import AgentOutput, AgentTask, ResearchMemo, ResearchMemoStatus
from app.db.session import reset_session_factory, session_scope
from app.graph.research_graph import build_research_graph
from app.ingestion.section_constants import (
    SECTION_COMPARABLES,
    SECTION_FUNDAMENTALS,
    SECTION_MACRO,
    SECTION_RISKS,
    SECTION_SENTIMENT,
    SECTION_SYNTHESIS,
)
from app.services.arxiv_client import reset_arxiv_client
from app.services.edgar_client import reset_edgar_client
from app.services.groq_client import reset_groq_client
from app.services.news_client import reset_news_client
from app.services.progress_publisher import publish_memo_terminal
from app.workers.celery_app import celery_app

#: Maps each memo section constant to its AgentTask.agent_type string —
#: used to source a failed/missing section's user-facing reason from that
#: agent's persisted AgentOutput.missing_fields (EXEC-04). Moved verbatim
#: from app/api/v1/research.py (deleted there in 06-05).
_AGENT_TYPE_BY_SECTION: dict[str, str] = {
    SECTION_FUNDAMENTALS: "FundamentalAnalysis",
    SECTION_SENTIMENT: "SentimentNLP",
    SECTION_RISKS: "RiskAssessment",
    SECTION_MACRO: "MacroSector",
    SECTION_COMPARABLES: "ComparableCompanies",
    SECTION_SYNTHESIS: "Synthesis",
}

#: Maps each memo section constant to its (output, status) AgentGraphState
#: field names — drives the full-section memo body assembly (EXEC-04: every
#: dispatched agent's section is present in the memo body, never omitted).
#: Moved verbatim from app/api/v1/research.py (deleted there in 06-05).
_SECTION_STATE_FIELDS: dict[str, tuple[str, str]] = {
    SECTION_FUNDAMENTALS: ("fundamentals_output", "fundamentals_status"),
    SECTION_SENTIMENT: ("sentiment_output", "sentiment_status"),
    SECTION_RISKS: ("risk_output", "risk_status"),
    SECTION_MACRO: ("macro_output", "macro_status"),
    SECTION_COMPARABLES: ("comparables_output", "comparables_status"),
    SECTION_SYNTHESIS: ("synthesis_output", "synthesis_status"),
}


def _extract_reason(missing_fields: object) -> str | None:
    """Normalize an ``AgentOutput.missing_fields`` JSON value into a single
    user-facing reason string.

    Moved verbatim from ``app/api/v1/research.py`` (deleted there in 06-05).
    ``missing_fields`` shapes vary across the 6 agents introduced across
    Phase 4/5 (a plain D-07 sentence string, a single-item list wrapping a
    D-07 sentence, or FundamentalAnalysis/Synthesis's older raw
    section/field-name list) — this normalizes any of those into one
    string, or ``None`` when there is nothing to report (SUCCESS/FULL).
    """
    if missing_fields is None:
        return None
    if isinstance(missing_fields, str):
        return missing_fields
    if isinstance(missing_fields, list):
        return "; ".join(str(item) for item in missing_fields) or None
    return str(missing_fields)


async def _run_research_async(
    memo_id: str, plan_id: str, ticker: str, user_id: str
) -> None:
    """Run the research graph and persist its result onto the existing memo.

    Opens its own DB session via ``session_scope()`` (never a request-scoped
    session — the task runs entirely outside any HTTP request lifecycle).
    Never creates a second ``ResearchMemo`` row: the PENDING row was already
    created by the dispatching endpoint (D-02), and ``parent_memo_id`` was
    set at that creation time.

    If the graph invocation or body assembly raises unexpectedly, the memo
    is forced to FAILED and a terminal FAILED event is published so a memo
    never hangs in RUNNING (belt-and-suspenders — the graph's own node
    functions never raise per Phase 4/5).
    """
    async with session_scope() as session:
        result = await session.execute(
            select(ResearchMemo).where(ResearchMemo.id == memo_id)
        )
        memo = result.scalar_one()
        memo.status = ResearchMemoStatus.RUNNING
        await session.commit()

        try:
            initial_state = {
                "plan_id": plan_id,
                "memo_id": memo_id,
                "ticker": ticker,
                "user_id": user_id,
                "session": session,
                "fundamentals_output": None,
                "fundamentals_status": "",
                "sentiment_output": None,
                "sentiment_status": "",
                "risk_output": None,
                "risk_status": "",
                "macro_output": None,
                "macro_status": "",
                "comparables_output": None,
                "comparables_status": "",
                "synthesis_output": None,
                "synthesis_status": "",
                "memo_status": "",
            }
            final_state = await build_research_graph().ainvoke(initial_state)

            # EXEC-04: assemble the memo body across EVERY dispatched
            # agent's section — a section is never dropped even when its
            # agent failed. A present output (SUCCESS or a degraded-but-
            # non-empty PARTIAL) is stored as-is; a None output (FAILED) is
            # replaced with an explicit marker carrying a user-facing reason
            # sourced from that agent's persisted AgentOutput.missing_fields,
            # never silently omitted.
            reason_result = await session.execute(
                select(
                    AgentTask.agent_type,
                    AgentTask.created_at,
                    AgentOutput.missing_fields,
                )
                .join(AgentOutput, AgentOutput.task_id == AgentTask.id)
                .where(
                    AgentTask.plan_id == plan_id,
                    AgentTask.agent_type.in_(_AGENT_TYPE_BY_SECTION.values()),
                )
                .order_by(AgentTask.created_at.desc())
            )
            reasons_by_agent_type: dict[str, str | None] = {}
            for agent_type, _created_at, missing_fields in reason_result.all():
                # Latest AgentTask per agent_type wins — a plan may have
                # prior runs' rows too (D-03 rerun lineage), and created_at
                # desc surfaces this run's row first.
                if agent_type not in reasons_by_agent_type:
                    reasons_by_agent_type[agent_type] = _extract_reason(
                        missing_fields
                    )

            body: dict[str, Any] = {}
            for section, (output_field, status_field) in _SECTION_STATE_FIELDS.items():
                output = final_state.get(output_field)
                if output is not None:
                    body[section] = output
                else:
                    agent_type = _AGENT_TYPE_BY_SECTION[section]
                    body[section] = {
                        "narrative": None,
                        "status": final_state.get(status_field),
                        "reason": reasons_by_agent_type.get(agent_type),
                    }

            memo.status = ResearchMemoStatus(final_state["memo_status"])
            memo.body = body
            await session.commit()
        except Exception:
            await session.rollback()
            memo.status = ResearchMemoStatus.FAILED
            await session.commit()

        await publish_memo_terminal(memo_id, memo.status.value)


@celery_app.task(name="run_research")
def run_research_task(memo_id: str, plan_id: str, ticker: str, user_id: str) -> None:
    """Celery entry point — synchronous wrapper around the async task body.

    Resets the DB engine/session-factory singleton AND every httpx-based
    service client singleton (EDGAR, news, arXiv, Groq) before running its
    own ``asyncio.run(...)``, so none of them reuse a connection bound to a
    previous task's (now-closed) event loop.
    """
    reset_session_factory()
    reset_edgar_client()
    reset_news_client()
    reset_arxiv_client()
    reset_groq_client()
    asyncio.run(_run_research_async(memo_id, plan_id, ticker, user_id))
