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

from ragas.dataset_schema import SingleTurnSample
from ragas.metrics import NonLLMContextPrecisionWithReference, NonLLMContextRecall

from app.eval.golden_set import GoldenCase
from app.ingestion.retriever import hybrid_retrieve

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


__all__ = [
    "DEFAULT_TOP_K",
    "RagasCaseScore",
    "score_case",
]
