"""Tests for app.services.ragas_eval_service (OBS-03, D-09).

Coverage (12-08-PLAN.md, Task 1 — single-case scoring):
  TestScoreCase:
    - identical reference passage scores context_recall above 0.0
    - entirely unrelated retrieved chunks score low without raising
    - returns a typed RagasCaseScore with the expected fields
    - case.user_id and the configured top_k pass through to hybrid_retrieve
      unchanged
    - an empty hybrid_retrieve result yields retrieved_count == 0 without
      raising (RAGAS's non-LLM context-recall implementation raises on an
      empty retrieved_contexts list; score_case must guard against this)
    - a full score_case run performs zero calls to the project's Groq entry
      point (T-12-08-QUOTA)

Coverage (Task 2 — whole-golden-set batch scoring + persistence):
  TestEvaluateGoldenSet:
    - scores every loaded case and persists one RagasEvalResult row per case
    - one raising case still yields a persisted NULL-scored row; the
      remaining cases score normally (T-12-08-FIXTUREDOS)
    - every row written in one pass shares one run_at value
    - returns RagasCaseScore results in golden-set order
    - a GoldenSetError from load_golden_set propagates rather than being
      swallowed here
    - a full pass over the (mocked) golden set performs zero Groq calls

Mocking policy: ``app.services.ragas_eval_service.hybrid_retrieve`` is the
retrieval boundary and is mocked in every test — no real ChromaDB/BM25/
cross-encoder pipeline runs. The DB session is REAL (test-postgres via the
``db_session`` fixture from ``tests/conftest.py``) for Task 2's persistence
tests, following ``tests/services/test_alert_evaluation_service.py``'s
convention. ``app.services.groq_client.call_groq`` is patched and asserted
never awaited.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RagasEvalResult
from app.eval.golden_set import GoldenCase, GoldenSetError
from app.services.ragas_eval_service import (
    DEFAULT_TOP_K,
    RagasCaseScore,
    evaluate_golden_set,
    score_case,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

REFERENCE_PASSAGE = (
    "Apple Inc. reported total net sales of $394.3 billion for fiscal year "
    "2023, representing a slight decline from the prior year driven primarily "
    "by softer demand in the Products segment, partially offset by continued "
    "growth in Services revenue across the installed base of active devices."
)

UNRELATED_PASSAGE = (
    "The migratory patterns of Arctic terns span from pole to pole each "
    "year, covering roughly seventy thousand kilometers round trip, making "
    "them the longest-distance migrants known in the animal kingdom by a "
    "wide margin over any other bird species studied to date."
)


def _make_case(**overrides: object) -> GoldenCase:
    defaults: dict[str, object] = {
        "case_id": "case-1",
        "query": "What were Apple's total net sales in fiscal 2023?",
        "ticker": "AAPL",
        "user_id": "",
        "provenance": "verbatim-edgar",
        "reference_contexts": (REFERENCE_PASSAGE,),
    }
    defaults.update(overrides)
    return GoldenCase(**defaults)  # type: ignore[arg-type]


def _retrieved(text: str, chunk_id: str = "chunk-1") -> dict:
    return {"id": chunk_id, "text": text, "metadata": {}, "score": 0.9}


# ---------------------------------------------------------------------------
# Task 1: score_case
# ---------------------------------------------------------------------------


class TestScoreCase:
    async def test_identical_reference_scores_recall_above_zero(self) -> None:
        case = _make_case()
        with patch(
            "app.services.ragas_eval_service.hybrid_retrieve",
            return_value=[_retrieved(REFERENCE_PASSAGE)],
        ):
            result = await score_case(case)

        assert result.context_recall is not None
        assert result.context_recall > 0.0

    async def test_unrelated_chunks_score_low_without_raising(self) -> None:
        case = _make_case()
        with patch(
            "app.services.ragas_eval_service.hybrid_retrieve",
            return_value=[_retrieved(UNRELATED_PASSAGE)],
        ):
            result = await score_case(case)

        assert result.context_precision is not None
        assert result.context_recall is not None
        assert result.context_precision < 0.5
        assert result.context_recall < 0.5

    async def test_returns_typed_result_with_expected_fields(self) -> None:
        case = _make_case()
        with patch(
            "app.services.ragas_eval_service.hybrid_retrieve",
            return_value=[_retrieved(REFERENCE_PASSAGE)],
        ):
            result = await score_case(case)

        assert isinstance(result, RagasCaseScore)
        assert result.case_id == "case-1"
        assert result.query == case.query
        assert result.ticker == "AAPL"
        assert isinstance(result.context_precision, float)
        assert isinstance(result.context_recall, float)
        assert result.retrieved_count == 1

    async def test_passes_user_id_and_top_k_through_unchanged(self) -> None:
        case = _make_case(user_id="", query="custom golden-set query")
        with patch(
            "app.services.ragas_eval_service.hybrid_retrieve",
            return_value=[_retrieved(REFERENCE_PASSAGE)],
        ) as mock_retrieve:
            await score_case(case, top_k=5)

        mock_retrieve.assert_called_once_with(case.query, case.user_id, top_k=5)

    async def test_empty_retrieval_yields_zero_count_without_raising(self) -> None:
        case = _make_case()
        with patch(
            "app.services.ragas_eval_service.hybrid_retrieve",
            return_value=[],
        ):
            result = await score_case(case)

        assert result.retrieved_count == 0
        assert result.context_precision is None
        assert result.context_recall is None

    async def test_scoring_makes_zero_groq_calls(self) -> None:
        case = _make_case()
        with (
            patch(
                "app.services.ragas_eval_service.hybrid_retrieve",
                return_value=[_retrieved(REFERENCE_PASSAGE)],
            ),
            patch("app.services.groq_client.call_groq", new_callable=AsyncMock) as mock_groq,
        ):
            await score_case(case)

        mock_groq.assert_not_awaited()


# ---------------------------------------------------------------------------
# Task 2: evaluate_golden_set
# ---------------------------------------------------------------------------


class TestEvaluateGoldenSet:
    async def test_scores_every_case_and_persists_one_row_each(
        self, db_session: AsyncSession
    ) -> None:
        cases = [_make_case(case_id="c1"), _make_case(case_id="c2", ticker="MSFT")]
        with (
            patch("app.services.ragas_eval_service.load_golden_set", return_value=cases),
            patch(
                "app.services.ragas_eval_service.hybrid_retrieve",
                return_value=[_retrieved(REFERENCE_PASSAGE)],
            ),
        ):
            results = await evaluate_golden_set(db_session)

        assert len(results) == 2
        rows = (await db_session.execute(select(RagasEvalResult))).scalars().all()
        assert len(rows) == 2
        assert {row.case_id for row in rows} == {"c1", "c2"}

    async def test_one_raising_case_yields_null_row_others_still_scored(
        self, db_session: AsyncSession
    ) -> None:
        cases = [_make_case(case_id="bad"), _make_case(case_id="good", ticker="MSFT")]

        async def _flaky_score_case(case: GoldenCase, top_k: int = DEFAULT_TOP_K) -> RagasCaseScore:
            if case.case_id == "bad":
                raise RuntimeError("simulated scoring failure")
            return RagasCaseScore(
                case_id=case.case_id,
                query=case.query,
                ticker=case.ticker,
                context_precision=0.8,
                context_recall=0.9,
                retrieved_count=1,
            )

        with (
            patch("app.services.ragas_eval_service.load_golden_set", return_value=cases),
            patch(
                "app.services.ragas_eval_service.score_case",
                side_effect=_flaky_score_case,
            ),
        ):
            results = await evaluate_golden_set(db_session)

        assert len(results) == 2
        rows = (await db_session.execute(select(RagasEvalResult))).scalars().all()

        bad_row = next(row for row in rows if row.case_id == "bad")
        assert bad_row.context_precision is None
        assert bad_row.context_recall is None
        assert bad_row.retrieved_count == 0

        good_row = next(row for row in rows if row.case_id == "good")
        assert good_row.context_precision == 0.8
        assert good_row.context_recall == 0.9

    async def test_all_rows_in_one_pass_share_one_run_at(self, db_session: AsyncSession) -> None:
        cases = [_make_case(case_id="c1"), _make_case(case_id="c2", ticker="MSFT")]
        with (
            patch("app.services.ragas_eval_service.load_golden_set", return_value=cases),
            patch(
                "app.services.ragas_eval_service.hybrid_retrieve",
                return_value=[_retrieved(REFERENCE_PASSAGE)],
            ),
        ):
            await evaluate_golden_set(db_session)

        rows = (await db_session.execute(select(RagasEvalResult))).scalars().all()
        run_ats = {row.run_at for row in rows}
        assert len(run_ats) == 1

    async def test_returns_results_in_golden_set_order(self, db_session: AsyncSession) -> None:
        cases = [
            _make_case(case_id="first"),
            _make_case(case_id="second", ticker="MSFT"),
            _make_case(case_id="third", ticker="TSLA"),
        ]
        with (
            patch("app.services.ragas_eval_service.load_golden_set", return_value=cases),
            patch(
                "app.services.ragas_eval_service.hybrid_retrieve",
                return_value=[_retrieved(REFERENCE_PASSAGE)],
            ),
        ):
            results = await evaluate_golden_set(db_session)

        assert [result.case_id for result in results] == ["first", "second", "third"]

    async def test_golden_set_error_propagates(self, db_session: AsyncSession) -> None:
        with patch(
            "app.services.ragas_eval_service.load_golden_set",
            side_effect=GoldenSetError("broken fixture"),
        ):
            with pytest.raises(GoldenSetError):
                await evaluate_golden_set(db_session)

    async def test_full_pass_makes_zero_groq_calls(self, db_session: AsyncSession) -> None:
        cases = [_make_case(case_id="c1")]
        with (
            patch("app.services.ragas_eval_service.load_golden_set", return_value=cases),
            patch(
                "app.services.ragas_eval_service.hybrid_retrieve",
                return_value=[_retrieved(REFERENCE_PASSAGE)],
            ),
            patch("app.services.groq_client.call_groq", new_callable=AsyncMock) as mock_groq,
        ):
            await evaluate_golden_set(db_session)

        mock_groq.assert_not_awaited()
