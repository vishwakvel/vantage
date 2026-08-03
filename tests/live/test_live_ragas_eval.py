"""Live smoke test proving the offline RAGAS evaluation genuinely scores
real retrieval against real infrastructure, with zero Groq client
construction (OBS-03, RESEARCH.md Pitfall 1/Pitfall 2).

Gated exactly like ``tests/live/test_live_service_clients.py``: the whole
module is skipped unless ``RUN_LIVE_TESTS=1`` is set, so an ordinary
``pytest`` run (CI, local dev) stays fully hermetic.

Prerequisites (all must hold before running this module):
    - The dev docker-compose stack's ``postgres`` and ``chromadb`` services
      must be running (``docker-compose up -d postgres chromadb``).
    - ``.env`` must be populated: ``app.core.config.Settings`` validates its
      required fields (``DATABASE_URL``, ``JWT_SECRET_KEY``,
      ``GROQ_API_KEY``) eagerly at instantiation even though nothing in this
      module ever calls Groq.
    - Alembic migrations must be at head (``alembic upgrade head``) so the
      ``ragas_eval_results`` table exists.
    - The golden set's three tickers (AAPL, MSFT, TSLA — see
      ``app/eval/ragas_golden_set.json``) must already be ingested into the
      ChromaDB corpus. This module deliberately triggers NO ingestion of its
      own (D-08) — it scores retrieval against whatever is already there.

What this module proves that ``tests/services/test_ragas_eval_service.py``
(plan 12-08's unit tests) cannot: those tests patch ``hybrid_retrieve``
directly, so they prove the plumbing — one row persisted per case, a failed
case falls back to NULL scores, the zero-model-call chokepoint contract —
under a mock. Only a real run against the real corpus can show whether the
non-LLM metrics produce meaningful, non-degenerate scores or the uniform
near-zero pattern that RESEARCH.md Pitfall 2 warns means the golden-set
fixture, not the retriever, is broken.

This module does NOT invoke the Celery task (``evaluate_retrieval_quality``,
plan 12-12) directly. The task layer's registration and never-raise
behaviour under a real exception are already covered by plan 12-12's unit
tests; invoking a Celery entry point from inside pytest would additionally
drag in its own event-loop and singleton-reset concerns for no incremental
coverage here. ``evaluate_golden_set`` — the service call underneath, which
is the part that actually touches real ChromaDB and real Postgres — is
exercised directly instead, exactly as the beat task exercises it. Do not
"fix" this by adding a Celery invocation.

Invocation::

    RUN_LIVE_TESTS=1 python -m pytest tests/live/test_live_ragas_eval.py -v -s
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

import app.services.groq_client as groq_client_module
from app.db.models import RagasEvalResult
from app.db.session import session_scope
from app.eval.golden_set import MIN_REFERENCE_CONTEXT_CHARS, load_golden_set
from app.services.ragas_eval_service import evaluate_golden_set

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="live API tests: set RUN_LIVE_TESTS=1 to run",
    ),
]


def _check(condition: bool, description: str) -> None:
    """Assert-and-print helper, mirroring scripts/smoke_alert_evaluation.py's
    ``_check`` — this module's terminal output is an artifact the plan's
    human checkpoint reads, not just a pass/fail."""
    if not condition:
        raise AssertionError(f"FAILED: {description}")
    print(f"  [OK] {description}")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


async def _delete_rows_from(run_at_floor: datetime) -> int:
    """Delete every RagasEvalResult row with ``run_at >= run_at_floor`` and
    return how many were removed. Shared by both tests' teardown."""
    async with session_scope() as session:
        stmt = select(RagasEvalResult).where(RagasEvalResult.run_at >= run_at_floor)
        rows = (await session.execute(stmt)).scalars().all()
        for row in rows:
            await session.delete(row)
        await session.commit()
        return len(rows)


async def _count_rows_from(run_at_floor: datetime) -> int:
    async with session_scope() as session:
        stmt = select(RagasEvalResult).where(RagasEvalResult.run_at >= run_at_floor)
        rows = (await session.execute(stmt)).scalars().all()
        return len(rows)


async def test_live_evaluate_golden_set_produces_non_degenerate_scores() -> None:
    """A full evaluate_golden_set pass against real ChromaDB/Postgres scores
    non-degenerately, the provenance split is readable from the run's own
    output, and rows persist under one identifiable run timestamp."""
    run_at_before = datetime.now(UTC)
    provenance_by_case_id = {case.case_id: case.provenance for case in load_golden_set()}

    async with session_scope() as session:
        results = await evaluate_golden_set(session)

    try:
        print("\n=== Per-case RAGAS scores (eyeball this) ===")
        for score in results:
            provenance = provenance_by_case_id.get(score.case_id, "unknown")
            print(
                f"  case={score.case_id} ticker={score.ticker} provenance={provenance} "
                f"context_precision={score.context_precision} "
                f"context_recall={score.context_recall} "
                f"retrieved_count={score.retrieved_count}"
            )

        def _is(case_id: str, wanted: str) -> bool:
            return provenance_by_case_id.get(case_id) == wanted

        verbatim_precisions = [
            s.context_precision
            for s in results
            if _is(s.case_id, "verbatim-edgar") and s.context_precision is not None
        ]
        verbatim_recalls = [
            s.context_recall
            for s in results
            if _is(s.case_id, "verbatim-edgar") and s.context_recall is not None
        ]
        authored_precisions = [
            s.context_precision
            for s in results
            if _is(s.case_id, "authored") and s.context_precision is not None
        ]
        authored_recalls = [
            s.context_recall
            for s in results
            if _is(s.case_id, "authored") and s.context_recall is not None
        ]

        print("\n=== Per-provenance mean scores (eyeball this) ===")
        print(
            f"  verbatim-edgar (n={len(verbatim_precisions)}): "
            f"mean context_precision={_mean(verbatim_precisions)} "
            f"mean context_recall={_mean(verbatim_recalls)}"
        )
        print(
            f"  authored       (n={len(authored_precisions)}): "
            f"mean context_precision={_mean(authored_precisions)} "
            f"mean context_recall={_mean(authored_recalls)}"
        )
        print(
            "  Per plan 12-04's provenance note: a low score concentrated on 'authored' "
            "cases points at the fixture; a low score on 'verbatim-edgar' cases is a "
            "genuine retrieval-quality signal about the hybrid retriever."
        )

        if all(score.retrieved_count == 0 for score in results):
            pytest.skip(
                "Every golden-set case retrieved zero chunks. This is an unmet "
                "environment precondition (an empty or unreachable ChromaDB corpus, "
                "or the golden set's tickers not yet ingested) — not a retrieval "
                "failure, so it is reported as a skip rather than a failed assertion."
            )

        _check(
            any((score.context_recall or 0.0) > 0.0 for score in results),
            "at least one case scored a strictly-positive context_recall — a "
            "uniformly-zero result across every case implicates the golden-set "
            f"fixture's passage format (app/eval/golden_set.py's "
            f"MIN_REFERENCE_CONTEXT_CHARS={MIN_REFERENCE_CONTEXT_CHARS} guard exists "
            "precisely to prevent this) before it implicates the retriever "
            "(RESEARCH.md Pitfall 2)",
        )

        for score in results:
            for value, metric_name in (
                (score.context_precision, "context_precision"),
                (score.context_recall, "context_recall"),
            ):
                _check(
                    value is None or 0.0 <= value <= 1.0,
                    f"case {score.case_id} {metric_name} ({value!r}) is absent or within [0, 1]",
                )

        persisted_count = await _count_rows_from(run_at_before)
        _check(
            persisted_count == len(results),
            f"exactly one RagasEvalResult row persisted per scored case "
            f"(expected {len(results)}, found {persisted_count})",
        )

        async with session_scope() as verify_session:
            stmt = select(RagasEvalResult).where(RagasEvalResult.run_at >= run_at_before)
            rows = (await verify_session.execute(stmt)).scalars().all()
            distinct_run_ats = {row.run_at for row in rows}
        _check(
            len(distinct_run_ats) == 1,
            "all persisted rows share a single run_at timestamp, identifying one run "
            f"(found {len(distinct_run_ats)} distinct value(s))",
        )
    finally:
        deleted = await _delete_rows_from(run_at_before)
        remaining = await _count_rows_from(run_at_before)
        _check(
            remaining == 0,
            "zero RagasEvalResult rows remain after teardown "
            f"(deleted {deleted}, {remaining} remain)",
        )


async def test_live_evaluate_golden_set_never_constructs_groq_client(monkeypatch) -> None:
    """The same real-infrastructure pass never constructs this project's
    lazy Groq SDK client — the live confirmation that the non-LLM metric
    variants (D-09) were used, not an LLM-judge path."""

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "app.services.groq_client._get_client was constructed during a live "
            "RAGAS eval run — the non-LLM metric variants must never reach the "
            "Groq chokepoint tests/test_boundaries.py enforces"
        )

    mock_get_client = MagicMock(side_effect=_fail_if_called)
    monkeypatch.setattr(groq_client_module, "_get_client", mock_get_client)

    # RESEARCH.md Pitfall 5: ragas transitively installs the openai SDK.
    # Clearing its credential for this test's duration means an accidental
    # judge-metric path fails loudly instead of quietly succeeding and
    # billing a third party.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    run_at_before = datetime.now(UTC)
    try:
        async with session_scope() as session:
            results = await evaluate_golden_set(session)

        _check(
            len(results) > 0,
            "evaluate_golden_set completed against real infrastructure and "
            "scored at least one case",
        )
        _check(
            mock_get_client.call_count == 0,
            "the Groq client factory (_get_client) was never invoked during the run",
        )
    finally:
        deleted = await _delete_rows_from(run_at_before)
        remaining = await _count_rows_from(run_at_before)
        _check(
            remaining == 0,
            "zero RagasEvalResult rows remain after teardown "
            f"(deleted {deleted}, {remaining} remain)",
        )
