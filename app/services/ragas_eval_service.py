"""Offline RAGAS retrieval-quality scoring for the golden set (OBS-03, D-09).

Scores the hand-curated golden set (``app/eval/golden_set.py``) against
``app/ingestion/retriever.py``'s existing hybrid retriever. Uses ONLY the two
``NonLLM``-prefixed RAGAS metric classes — ``NonLLMContextPrecisionWithReference``
and ``NonLLMContextRecall`` — so a scheduled run consumes no Groq quota
(D-09): these metrics compare full retrieved-chunk text against full
reference-passage text via a normalized Levenshtein similarity at a 0.5
threshold (rapidfuzz-backed, no evaluator LLM), which is exactly why the
golden set stores verbatim passages rather than keywords
(12-RESEARCH.md Pitfall 2) — never import the bare/short metric names or any
``LLMContextPrecisionWithReference``/``ContextRecall`` variant; those require
an evaluator-LLM constructor argument and would make a real model call per
case, precisely what D-09 forbids.

Scores are persisted (``RagasEvalResult`` rows) for later inspection only —
this module adds no route, no Pydantic response model, and no UI (D-10).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ragas.dataset_schema import SingleTurnSample
from ragas.metrics import NonLLMContextPrecisionWithReference, NonLLMContextRecall

from app.db.models import RagasEvalResult
from app.eval.golden_set import GoldenCase, load_golden_set
from app.ingestion.retriever import hybrid_retrieve

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Matches hybrid_retrieve's own default (app/ingestion/retriever.py).
DEFAULT_TOP_K: int = 10

# ---------------------------------------------------------------------------
# Metric instances — constructed with NO arguments (no evaluator_llm, ever).
# Both are pure rapidfuzz-backed string-similarity scorers; they hold no
# network/event-loop-bound resource, so a single module-level instance is
# safe to reuse across calls (unlike the lazy-singleton/reset_*() pattern
# groq_client.py needs for its AsyncGroq client).
# ---------------------------------------------------------------------------

_precision_metric = NonLLMContextPrecisionWithReference()
_recall_metric = NonLLMContextRecall()


@dataclass(frozen=True)
class RagasCaseScore:
    """Result of scoring one golden-set case.

    ``context_precision``/``context_recall`` are optional so a case that
    could not be scored (e.g. retrieval returned nothing) persists a NULL
    rather than a fake ``0.0`` that would be indistinguishable from a
    genuine zero score.
    """

    case_id: str
    query: str
    ticker: str
    context_precision: float | None
    context_recall: float | None
    retrieved_count: int


async def score_case(case: GoldenCase, top_k: int = DEFAULT_TOP_K) -> RagasCaseScore:
    """Retrieve and score one golden-set case with the non-LLM metrics.

    ``hybrid_retrieve`` is synchronous, so it is offloaded via
    ``asyncio.to_thread`` — mirroring the convention
    ``fundamental_analysis._collect_anomalies`` uses for ``detect_anomalies``
    — so it never blocks the event loop.

    When retrieval returns no chunks at all, both score fields are ``None``
    and ``retrieved_count`` is 0 without calling either metric: the installed
    RAGAS release's non-LLM context-recall implementation raises on an empty
    ``retrieved_contexts`` list (``max() arg is an empty sequence``), so this
    case must never reach the metric call.
    """
    results = await asyncio.to_thread(hybrid_retrieve, case.query, case.user_id, top_k=top_k)
    retrieved_texts = [result["text"] for result in results]
    retrieved_count = len(retrieved_texts)

    if not retrieved_texts:
        return RagasCaseScore(
            case_id=case.case_id,
            query=case.query,
            ticker=case.ticker,
            context_precision=None,
            context_recall=None,
            retrieved_count=0,
        )

    # DeprecationWarning expected here (single_turn_ascore is deprecated in
    # favor of RAGAS 0.4's collections/ascore API, which does not yet offer a
    # non-LLM equivalent of these two metrics — this is the only documented
    # non-LLM path in the installed release). Not suppressed, not a failure.
    sample = SingleTurnSample(
        retrieved_contexts=retrieved_texts,
        reference_contexts=list(case.reference_contexts),
    )
    context_precision = await _precision_metric.single_turn_ascore(sample)
    context_recall = await _recall_metric.single_turn_ascore(sample)

    return RagasCaseScore(
        case_id=case.case_id,
        query=case.query,
        ticker=case.ticker,
        context_precision=context_precision,
        context_recall=context_recall,
        retrieved_count=retrieved_count,
    )


async def evaluate_golden_set(
    session: AsyncSession, top_k: int = DEFAULT_TOP_K
) -> list[RagasCaseScore]:
    """Score the whole golden set, persisting one row per case, and commit.

    Mirrors ``alert_evaluation_service.evaluate_all_rules``'s session-in /
    summary-out shape: the caller owns the session lifecycle; this function
    does the work and commits once at the end.

    One ``run_at`` timestamp is computed ONCE before the loop and reused for
    every row so a pass is identifiable as one run. Each case is scored
    inside a ``try/except Exception`` — one unscoreable case can never abort
    the pass, mirroring ``synthesis._parse_contradictions``'s "skip one
    malformed item, keep the rest" rule — and falls back to a
    ``RagasCaseScore`` with both score fields ``None`` and
    ``retrieved_count`` 0. A ``RagasEvalResult`` row is added for every case,
    including failed ones.

    ``load_golden_set()`` is deliberately called OUTSIDE that try/except: a
    broken fixture is a developer error affecting the whole run, not a
    per-case condition, so ``GoldenSetError`` propagates to the caller (the
    Celery beat task added in a later plan owns the outer swallow-and-log).

    Only a one-line info-level summary (case count, failure count) is
    logged after the loop — no query text or per-case scores, which would be
    noise on a daily job.
    """
    cases = load_golden_set()
    run_at = datetime.now(UTC)

    results: list[RagasCaseScore] = []
    failed_count = 0

    for case in cases:
        try:
            score = await score_case(case, top_k=top_k)
        except Exception:
            logger.exception("Failed to score golden-set case %s", case.case_id)
            score = RagasCaseScore(
                case_id=case.case_id,
                query=case.query,
                ticker=case.ticker,
                context_precision=None,
                context_recall=None,
                retrieved_count=0,
            )
            failed_count += 1

        results.append(score)
        session.add(
            RagasEvalResult(
                run_at=run_at,
                case_id=score.case_id,
                query=score.query,
                ticker=score.ticker,
                context_precision=score.context_precision,
                context_recall=score.context_recall,
                retrieved_count=score.retrieved_count,
            )
        )

    await session.commit()

    logger.info(
        "RAGAS golden-set eval run complete: %d case(s), %d failed to score",
        len(cases),
        failed_count,
    )

    return results


__all__ = [
    "DEFAULT_TOP_K",
    "RagasCaseScore",
    "score_case",
    "evaluate_golden_set",
]
