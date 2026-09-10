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
EDGAR/news/arXiv/FRED/Groq clients) before its own ``asyncio.run(...)``, so
none of them are reused from a prior (now-closed) task's event loop — reusing
an asyncpg connection or httpx.AsyncClient bound to a closed loop raises
"RuntimeError: Event loop is closed" (see
``app/db/session.py::reset_session_factory`` docstring, which the other
five resets mirror exactly). The ambient plan-id scope (see
``_run_research_async``) is set inside the async body precisely so it
cannot outlive this task's own context — it lives in the Task-local
context copy ``asyncio.run`` creates, not in this module-level state.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from app.db.models import AgentOutput, AgentTask, ResearchMemo, ResearchMemoStatus
from app.db.session import reset_session_factory, session_scope
from app.graph.research_graph import build_research_graph
from app.ingestion.section_constants import (
    SECTION_ANOMALIES,
    SECTION_COMPARABLES,
    SECTION_CONTRADICTIONS,
    SECTION_COST,
    SECTION_FUNDAMENTALS,
    SECTION_MACRO,
    SECTION_RISKS,
    SECTION_SENTIMENT,
    SECTION_SYNTHESIS,
)
from app.services.api_call_counter import read_and_clear_api_call_count, set_current_plan_id
from app.services.arxiv_client import reset_arxiv_client
from app.services.edgar_client import reset_edgar_client
from app.services.fred_client import reset_fred_client
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

    Sets the ambient plan-id scope first thing to establish the plan-scoped
    external-API counter context (D-05, OBS-02) for the whole graph run —
    this is the second of the two entry points ``api_call_counter.py``
    names (the first is ``POST /research``'s EDGAR ingestion, set in
    ``app/api/v1/research.py``); without it the five in-graph service
    clients would resolve no plan id and silently no-op.

    Then opens its own DB session via the session-scope context manager
    (never a request-scoped session — the task runs entirely outside any
    HTTP request lifecycle). Never creates a second ``ResearchMemo`` row:
    the PENDING row was already created by the dispatching endpoint (D-02),
    and ``parent_memo_id`` was set at that creation time.

    If the graph invocation or body assembly raises unexpectedly, the memo
    is forced to FAILED and a terminal FAILED event is published so a memo
    never hangs in RUNNING (belt-and-suspenders — the graph's own node
    functions never raise per Phase 4/5).
    """
    set_current_plan_id(plan_id)
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
                    AgentOutput.prompt_tokens,
                    AgentOutput.completion_tokens,
                )
                .join(AgentOutput, AgentOutput.task_id == AgentTask.id)
                .where(
                    AgentTask.plan_id == plan_id,
                    AgentTask.agent_type.in_(_AGENT_TYPE_BY_SECTION.values()),
                )
                .order_by(AgentTask.created_at.desc())
            )
            reasons_by_agent_type: dict[str, str | None] = {}
            total_tokens = 0
            for (
                agent_type,
                _created_at,
                missing_fields,
                prompt_tokens,
                completion_tokens,
            ) in reason_result.all():
                # Latest AgentTask per agent_type wins — a plan may have
                # prior runs' rows too (D-03 rerun lineage), and created_at
                # desc surfaces this run's row first.
                if agent_type not in reasons_by_agent_type:
                    reasons_by_agent_type[agent_type] = _extract_reason(
                        missing_fields
                    )
                    # MEMO-06/OBS-02: this run's own total generation cost —
                    # deliberately token counts only, no monetary figure
                    # (D-04, Groq free tier). Guarded with `or 0` because
                    # these columns are nullable (12-02) and every agent has
                    # failure branches that write an AgentOutput row without
                    # ever completing a Groq call.
                    total_tokens += (prompt_tokens or 0) + (completion_tokens or 0)

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

            # MEMO-04: the synthesis section always exposes a contradictions
            # list — on the SUCCESS path it is already present from
            # synthesis_output (Plan 02); on the FAILED-marker path above it
            # is backfilled here so body.synthesis.contradictions is never
            # missing/undefined for the frontend (EXEC-04 precedent, applied
            # one level down to this nested key).
            body[SECTION_SYNTHESIS].setdefault(SECTION_CONTRADICTIONS, [])

            # METRIC-02: the fundamentals section always exposes an
            # anomalies list — on the SUCCESS path the key is already
            # present from fundamentals_output (plan 09-06), but the
            # FAILED-marker branch above replaces the section body
            # wholesale with narrative/status/reason and would otherwise
            # leave body.fundamentals.anomalies undefined for the frontend.
            body[SECTION_FUNDAMENTALS].setdefault(SECTION_ANOMALIES, [])

            # MEMO-06/OBS-02: this run's total generation cost, assembled
            # unconditionally so the key always survives into the persisted
            # memo (Phase 7 body-assembly guarantee) — a run in which every
            # agent failed still persists a zero token total rather than a
            # missing key. Per D-04 this is deliberately token counts plus
            # external API call counts only, never a monetary figure. The
            # token total comes from this run's own AgentOutput rows
            # (accumulated above); the call count comes from the
            # plan-scoped Redis counter spanning EDGAR ingestion and the
            # graph run (12-06/12-07), read exactly once here and cleared
            # so a rerun of the same plan starts from zero.
            api_calls = await read_and_clear_api_call_count(plan_id)
            body[SECTION_COST] = {"tokens": total_tokens, "api_calls": api_calls}

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
    service client singleton (EDGAR, news, arXiv, FRED, Groq) before running
    its own ``asyncio.run(...)``, so none of them reuse a connection bound to
    a previous task's (now-closed) event loop.
    """
    reset_session_factory()
    reset_edgar_client()
    reset_news_client()
    reset_arxiv_client()
    reset_fred_client()
    reset_groq_client()
    asyncio.run(_run_research_async(memo_id, plan_id, ticker, user_id))
