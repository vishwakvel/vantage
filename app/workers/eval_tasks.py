"""Celery periodic task evaluating retrieval quality over the golden set (OBS-03).

This is the ONE beat-scheduled entry point for offline retrieval-quality
evaluation (OBS-03). Plan 12-12's ``beat_schedule`` entry
(``app/workers/celery_app.py``) is the only caller of this task; nothing else
in the codebase enqueues it.

Event-loop safety: this task resets the module-level singleton that wraps a
persistent async network client and is bound to a prior task's (now closed)
event loop before its own ``asyncio.run(...)`` — the exact "RuntimeError:
Event loop is closed" hazard recorded as a Phase 6 lesson in PROJECT.md and
mirrored by ``app/workers/tasks.py::run_research_task`` and
``app/workers/alert_tasks.py``. One singleton is reset here:

- ``reset_session_factory()`` (``app/db/session.py``) — the asyncpg
  engine/session factory. See its docstring for the full "Event loop is
  closed" rationale this reset exists to prevent.

Deliberately NOT reset here: the Groq, EDGAR, news, and arXiv clients. No
code path reachable from this task touches any of them — the evaluation
makes zero LLM calls by design (D-09) and reads only already-ingested chunks
through ``app.ingestion.retriever.hybrid_retrieve``, which is ChromaDB-backed
and synchronous, so it holds no async client bound to a prior task's closed
event loop.

This task produces no user-facing output (D-10): it persists
``RagasEvalResult`` rows and logs a one-line summary, and there is no route
or UI reading them.
"""

from __future__ import annotations

import asyncio
import logging

from app.db.session import reset_session_factory, session_scope
from app.eval.golden_set import GoldenSetError
from app.services.ragas_eval_service import evaluate_golden_set
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _evaluate_retrieval_quality_async() -> None:
    """Open a session and delegate to ``evaluate_golden_set`` for one tick.

    Logs the scored-case count at info level — a per-tick count is enough to
    operate the job and carries no query text or per-case scores.

    A ``GoldenSetError`` (the golden-set fixture itself is structurally
    broken) is caught in its own clause ahead of the generic one, with a log
    message that names it as a fixture problem rather than a scoring
    problem — ``evaluate_golden_set`` deliberately lets this error propagate
    so this layer can make that distinction.

    Any other exception escaping ``evaluate_golden_set`` (an infrastructure
    failure such as an unreachable DB — the scoring service already isolates
    per-case failures internally, so anything else reaching here is not a
    scoring problem) is logged at error level and swallowed: a task that
    raises would still be re-scheduled by beat, but an unhandled traceback
    per tick is operational noise.
    """
    try:
        async with session_scope() as session:
            scores = await evaluate_golden_set(session)
        logger.info("RAGAS golden-set eval tick complete: %d case(s) scored", len(scores))
    except GoldenSetError:
        logger.exception("RAGAS golden-set eval tick failed: golden-set fixture is broken")
    except Exception:
        logger.exception("RAGAS golden-set eval tick failed")


@celery_app.task(name="evaluate_retrieval_quality")
def evaluate_retrieval_quality_task() -> None:
    """Celery beat entry point — synchronous wrapper around the async body.

    Resets the DB engine/session-factory singleton before running the async
    body under its own fresh event loop, so it never reuses a connection
    bound to a previous tick's (now-closed) loop — the same event-loop-safety
    contract ``app/workers/alert_tasks.py`` and
    ``app/workers/tasks.py::run_research_task`` follow.
    """
    reset_session_factory()
    asyncio.run(_evaluate_retrieval_quality_async())


__all__ = ["evaluate_retrieval_quality_task"]
