"""Celery periodic task evaluating every enabled AlertRule (WATCH-09, D-04).

This is the ONE beat-scheduled entry point for all three alert rule types
(NEW_FILING, PRICE_MOVE, SCHEDULED) — per D-04, a single periodic task on a
short tick evaluates every enabled rule of every type in one pass, because
each type's own due-ness logic already lives inside
``app.services.alert_evaluation_service.evaluate_all_rules`` (PRICE_MOVE and
NEW_FILING check every tick; SCHEDULED checks every tick but only fires once
its cadence has elapsed since ``state["last_triggered_at"]``). Plan 11-05's
``beat_schedule`` entry (``app/workers/celery_app.py``) is the only caller of
this task; nothing else in the codebase enqueues it.

Event-loop safety: this task resets every module-level singleton that wraps
a persistent async network client and is bound to a prior task's (now
closed) event loop before its own ``asyncio.run(...)`` — the exact
"RuntimeError: Event loop is closed" hazard recorded as a Phase 6 lesson in
PROJECT.md and mirrored by ``app/workers/tasks.py::run_research_task``. Two
singletons are reset here:

- ``reset_session_factory()`` (``app/db/session.py``) — the asyncpg
  engine/session factory.
- ``reset_edgar_client()`` (``app/services/edgar_client.py``) — the two
  httpx.AsyncClient instances NEW_FILING evaluation uses.

One singleton is deliberately NOT reset: ``app.services.live_price_source``
(the market-data price source PRICE_MOVE evaluation uses). It is
synchronous yfinance dispatched to a worker thread via ``asyncio.to_thread``,
so it holds no client object bound to a prior task's closed event loop —
the same reason ``app/workers/tasks.py::run_research_task`` never resets
``financial_metrics_source``. See ``app/services/live_price_source.py``'s
module docstring for the full rationale; that module intentionally exposes
no ``reset_*()`` function at all, so there is nothing to call here.

Also NOT called here: ``reset_news_client``, ``reset_arxiv_client``, and
``reset_groq_client``. This task's code path touches none of those clients —
D-01 (11-CONTEXT.md) is explicit that a SCHEDULED rule firing never launches
a research run, so no code path reachable from this module ever needs the
research pipeline's news/arXiv/Groq clients.
"""

from __future__ import annotations

import asyncio
import logging

from app.db.session import reset_session_factory, session_scope
from app.services.alert_evaluation_service import evaluate_all_rules
from app.services.edgar_client import reset_edgar_client
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _evaluate_all_rules_async() -> None:
    """Open a session and delegate to ``evaluate_all_rules`` for one tick.

    Logs the fired count at info level — a per-tick count is enough to
    operate the job and carries no rule ids, tickers, or user ids (no
    user-linkable data in this log line).

    Any exception escaping ``evaluate_all_rules`` (an infrastructure
    failure such as an unreachable DB — the evaluator already isolates
    per-rule failures internally, so anything reaching here is not a rule
    problem) is logged at error level and swallowed: a task that raises
    would still be re-scheduled by beat, but an unhandled traceback per
    tick is operational noise.
    """
    try:
        async with session_scope() as session:
            fired = await evaluate_all_rules(session)
        logger.info("Alert rule evaluation tick complete: %d notification(s) fired", fired)
    except Exception:
        logger.exception("Alert rule evaluation tick failed")


@celery_app.task(name="evaluate_alert_rules")
def evaluate_alert_rules_task() -> None:
    """Celery beat entry point — synchronous wrapper around the async body.

    Resets the DB engine/session-factory singleton and the EDGAR client
    singleton before running the async body under its own fresh event loop,
    so neither reuses a connection bound to a previous tick's (now-closed)
    loop — the same event-loop-safety contract
    ``app/workers/tasks.py::run_research_task`` follows.
    """
    reset_session_factory()
    reset_edgar_client()
    asyncio.run(_evaluate_all_rules_async())


__all__ = ["evaluate_alert_rules_task"]
