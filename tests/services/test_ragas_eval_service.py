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

Mocking policy: ``app.services.ragas_eval_service.hybrid_retrieve`` is the
retrieval boundary and is mocked in every test — no real ChromaDB/BM25/
cross-encoder pipeline runs. ``app.services.groq_client.call_groq`` is
patched and asserted never awaited.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.eval.golden_set import GoldenCase
from app.services.ragas_eval_service import RagasCaseScore, score_case

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
            patch(
                "app.services.groq_client.call_groq", new_callable=AsyncMock
            ) as mock_groq,
        ):
            await score_case(case)

        mock_groq.assert_not_awaited()
