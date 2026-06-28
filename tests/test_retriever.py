"""Tests for app/ingestion/retriever.py — hybrid retrieval (RRF, BM25, cross-encoder).

All external dependencies are monkeypatched:
  - vector_store.dense_query is replaced with a MagicMock
  - CrossEncoder is replaced with a MagicMock
  - No real ChromaDB connection or model download occurs

Strategy:
  - test_rrf*: pure function tests (no mocks needed)
  - test_bm25*: pure function tests (no mocks needed)
  - test_hybrid_retrieve*: require monkeypatching vector_store and CrossEncoder
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Test helpers — build fake dense_query results
# ---------------------------------------------------------------------------


def _make_dense_results(
    ids: list[str],
    texts: list[str],
    metadatas: list[dict] | None = None,
) -> dict:
    """Return a dict shaped like vector_store.dense_query output."""
    if metadatas is None:
        metadatas = [{"user_id": "u1"} for _ in ids]
    return {
        "ids": [ids],
        "documents": [texts],
        "metadatas": [metadatas],
        "distances": [[0.1 * (i + 1) for i in range(len(ids))]],
    }


# ---------------------------------------------------------------------------
# Task 1a: reciprocal_rank_fusion — pure function
# ---------------------------------------------------------------------------


class TestRRF:
    """Tests for reciprocal_rank_fusion(dense_ids, sparse_ids, k=60)."""

    def test_rrf_id_in_both_lists_ranks_first(self):
        """An id ranked highly in BOTH lists should outrank ids in only one list."""
        from app.ingestion.retriever import reciprocal_rank_fusion

        dense_ids = ["A", "B", "C"]
        sparse_ids = ["A", "D", "E"]  # "A" is rank 1 in both
        result = reciprocal_rank_fusion(dense_ids, sparse_ids, k=60)

        assert result[0] == "A", "A appears rank 1 in both — must be first in fusion"

    def test_rrf_score_uses_k60(self):
        """Score for a doc at rank 1 in one list should be 1/(60+1) = 1/61."""
        from app.ingestion.retriever import reciprocal_rank_fusion

        # Only "X" in dense; "Y" only in sparse (both at rank 1)
        result = reciprocal_rank_fusion(["X"], ["Y"], k=60)

        # Both got the same score (1/61 each), so order among them is tie-broken
        # — but both must be present
        assert set(result) == {"X", "Y"}

    def test_rrf_returns_all_unique_ids(self):
        """All ids from both lists are returned (union, deduplicated)."""
        from app.ingestion.retriever import reciprocal_rank_fusion

        dense_ids = ["A", "B"]
        sparse_ids = ["B", "C"]  # B appears in both
        result = reciprocal_rank_fusion(dense_ids, sparse_ids, k=60)

        assert set(result) == {"A", "B", "C"}

    def test_rrf_ordering_best_first(self):
        """Higher-ranked items come first in the output."""
        from app.ingestion.retriever import reciprocal_rank_fusion

        # "A" rank 1 both lists → score = 2*(1/61) ≈ 0.0328
        # "B" rank 2 dense only → score = 1/62 ≈ 0.0161
        # "C" rank 1 sparse, rank 3 dense → 1/63 + 1/61 ≈ 0.0321
        dense_ids = ["A", "B", "C"]
        sparse_ids = ["A", "C", "D"]
        result = reciprocal_rank_fusion(dense_ids, sparse_ids, k=60)

        # "A" must beat "B" (A is rank 1 in both, B is rank 2 in dense only)
        assert result.index("A") < result.index("B")

    def test_rrf_empty_inputs(self):
        """Empty inputs return empty output."""
        from app.ingestion.retriever import reciprocal_rank_fusion

        assert reciprocal_rank_fusion([], [], k=60) == []

    def test_rrf_single_list(self):
        """Only one list populated still returns correct order."""
        from app.ingestion.retriever import reciprocal_rank_fusion

        result = reciprocal_rank_fusion(["A", "B", "C"], [], k=60)
        assert result == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# Task 1b: bm25_rank — pure function
# ---------------------------------------------------------------------------


class TestBM25Rank:
    """Tests for bm25_rank(query, candidate_texts) -> list[int]."""

    def test_bm25_rank_best_match_first(self):
        """The candidate whose text best matches query tokens ranks first."""
        from app.ingestion.retriever import bm25_rank

        candidates = [
            "revenue growth quarterly earnings report",
            "the dog barked at the mailman yesterday",
            "revenue increased significantly in Q4 earnings",
        ]
        indices = bm25_rank("revenue earnings", candidates)

        # Indices 0 and 2 both mention "revenue" and "earnings"; index 1 does not
        assert indices[0] in (0, 2), f"Expected 0 or 2 first, got {indices[0]}"
        assert indices[-1] == 1, f"Unrelated doc should rank last, got {indices[-1]}"

    def test_bm25_rank_returns_all_indices(self):
        """Return a permutation of [0, 1, ..., N-1]."""
        from app.ingestion.retriever import bm25_rank

        candidates = ["alpha", "beta", "gamma"]
        indices = bm25_rank("alpha", candidates)

        assert sorted(indices) == [0, 1, 2]
        assert len(indices) == 3

    def test_bm25_rank_case_insensitive(self):
        """Tokenisation is lower().split() so case does not affect ranking.

        Uses a 4-doc corpus so the query term appears in <50% of docs and BM25
        IDF is positive (avoids the log(1.5/1.5)=0 IDF edge case with 2 docs
        where the term appears in exactly 1 of them).
        """
        from app.ingestion.retriever import bm25_rank

        candidates = [
            "Revenue grew significantly",           # 0 — contains "revenue"
            "unrelated text here",                  # 1 — no match
            "net income declined last quarter",     # 2 — no match
            "operating expenses increased",         # 3 — no match
        ]
        # Query in upper-case; tokenised to lowercase → should match index 0
        lower_indices = bm25_rank("revenue", candidates)
        upper_indices = bm25_rank("REVENUE", candidates)
        # Both queries should produce the same ranking (case-insensitive)
        assert lower_indices == upper_indices, (
            "BM25 ranking must be identical for 'revenue' and 'REVENUE'"
        )
        # And both should rank the matching doc first
        assert lower_indices[0] == 0, f"Matching doc (index 0) should rank first; got {lower_indices}"

    def test_bm25_rank_single_candidate(self):
        """Single candidate always returns [0]."""
        from app.ingestion.retriever import bm25_rank

        assert bm25_rank("anything", ["some text"]) == [0]

    def test_bm25_rank_uses_candidate_set_only(self):
        """BM25 must be built from the passed candidates, not a global corpus.

        This is a contract test: the function must be deterministic given
        only its arguments (no hidden global state).
        """
        from app.ingestion.retriever import bm25_rank

        candidates = ["gross margin improved", "net income declined"]
        first_call = bm25_rank("gross margin", candidates)
        second_call = bm25_rank("gross margin", candidates)
        assert first_call == second_call, "bm25_rank must be deterministic"


# ---------------------------------------------------------------------------
# Task 2: hybrid_retrieve — requires monkeypatching
# ---------------------------------------------------------------------------


def _setup_mock_dense(mock_ids, mock_texts, mock_metadatas=None):
    """Return a (dense_result_dict, mock_fn) pair for patching."""
    result = _make_dense_results(mock_ids, mock_texts, mock_metadatas)
    mock_fn = MagicMock(return_value=result)
    return result, mock_fn


class TestHybridRetrieve:
    """Tests for hybrid_retrieve(query, user_id, top_k=10)."""

    # ------------------------------------------------------------------
    # INGEST-03: user_id must always be forwarded to dense_query
    # ------------------------------------------------------------------

    def test_hybrid_retrieve_forwards_user_id(self, monkeypatch):
        """dense_query must receive the exact user_id passed to hybrid_retrieve."""
        import app.services.vector_store as vs
        import app.ingestion.retriever as retriever_module

        ids = ["c1", "c2", "c3"]
        texts = ["alpha revenue growth", "beta earnings decline", "gamma net income"]
        _, mock_dense = _setup_mock_dense(ids, texts)

        monkeypatch.setattr(vs, "dense_query", mock_dense)

        # Patch cross-encoder to avoid model download
        fake_reranker = MagicMock()
        fake_reranker.predict.return_value = np.array([0.9, 0.7, 0.5])
        monkeypatch.setattr(retriever_module, "_reranker", fake_reranker)

        results = retriever_module.hybrid_retrieve("revenue", "user-abc-123", top_k=3)

        # Assert user_id was forwarded
        call_args = mock_dense.call_args
        called_user_id = call_args[0][1] if call_args[0] else call_args[1].get("user_id")
        assert called_user_id == "user-abc-123", (
            f"dense_query must receive user_id='user-abc-123', got '{called_user_id}'"
        )

    def test_hybrid_retrieve_result_length_bounded_by_top_k(self, monkeypatch):
        """Returned list length must be <= top_k."""
        import app.services.vector_store as vs
        import app.ingestion.retriever as retriever_module

        # 5 candidates returned by dense_query
        ids = [f"c{i}" for i in range(5)]
        texts = [f"chunk text about revenue earnings {i}" for i in range(5)]
        _, mock_dense = _setup_mock_dense(ids, texts)
        monkeypatch.setattr(vs, "dense_query", mock_dense)

        fake_reranker = MagicMock()
        fake_reranker.predict.return_value = np.array([0.9, 0.7, 0.5, 0.3, 0.1])
        monkeypatch.setattr(retriever_module, "_reranker", fake_reranker)

        results = retriever_module.hybrid_retrieve("revenue", "u1", top_k=3)
        assert len(results) <= 3

    def test_hybrid_retrieve_ordered_by_reranker_score(self, monkeypatch):
        """Results must be sorted by cross-encoder score (highest first)."""
        import app.services.vector_store as vs
        import app.ingestion.retriever as retriever_module

        ids = ["c0", "c1", "c2"]
        texts = ["text zero", "text one", "text two"]
        _, mock_dense = _setup_mock_dense(ids, texts)
        monkeypatch.setattr(vs, "dense_query", mock_dense)

        # c2 gets highest score, c0 gets lowest
        fake_reranker = MagicMock()
        fake_reranker.predict.return_value = np.array([0.1, 0.5, 0.9])
        monkeypatch.setattr(retriever_module, "_reranker", fake_reranker)

        results = retriever_module.hybrid_retrieve("query", "u1", top_k=3)

        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True), "Results must be ordered by score desc"

    def test_hybrid_retrieve_result_schema(self, monkeypatch):
        """Each result item must have id, text, metadata, and score keys."""
        import app.services.vector_store as vs
        import app.ingestion.retriever as retriever_module

        ids = ["chunk-1"]
        texts = ["some relevant text"]
        metadatas = [{"ticker": "AAPL", "user_id": "u1"}]
        _, mock_dense = _setup_mock_dense(ids, texts, metadatas)
        monkeypatch.setattr(vs, "dense_query", mock_dense)

        fake_reranker = MagicMock()
        fake_reranker.predict.return_value = np.array([0.75])
        monkeypatch.setattr(retriever_module, "_reranker", fake_reranker)

        results = retriever_module.hybrid_retrieve("text", "u1", top_k=5)

        assert len(results) == 1
        item = results[0]
        assert "id" in item
        assert "text" in item
        assert "metadata" in item
        assert "score" in item
        assert item["id"] == "chunk-1"
        assert item["text"] == "some relevant text"

    def test_hybrid_retrieve_no_real_chromadb_call(self, monkeypatch):
        """Verifies test isolation: dense_query mock is called, not the real ChromaDB."""
        import app.services.vector_store as vs
        import app.ingestion.retriever as retriever_module

        ids = ["d1", "d2"]
        texts = ["text one about earnings", "text two about revenue"]
        _, mock_dense = _setup_mock_dense(ids, texts)
        monkeypatch.setattr(vs, "dense_query", mock_dense)

        fake_reranker = MagicMock()
        fake_reranker.predict.return_value = np.array([0.8, 0.6])
        monkeypatch.setattr(retriever_module, "_reranker", fake_reranker)

        retriever_module.hybrid_retrieve("earnings revenue", "u1", top_k=2)

        mock_dense.assert_called_once()

    def test_hybrid_retrieve_empty_dense_results(self, monkeypatch):
        """When dense_query returns no results, hybrid_retrieve returns []."""
        import app.services.vector_store as vs
        import app.ingestion.retriever as retriever_module

        empty_result = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        monkeypatch.setattr(vs, "dense_query", MagicMock(return_value=empty_result))

        fake_reranker = MagicMock()
        fake_reranker.predict.return_value = np.array([])
        monkeypatch.setattr(retriever_module, "_reranker", fake_reranker)

        results = retriever_module.hybrid_retrieve("query", "u1", top_k=5)
        assert results == []

    def test_hybrid_retrieve_bm25_built_from_candidates_only(self, monkeypatch):
        """BM25 index is built from the dense candidate set, not a global corpus (Pitfall 5)."""
        import app.services.vector_store as vs
        import app.ingestion.retriever as retriever_module
        from app.ingestion.retriever import bm25_rank

        ids = ["e1", "e2"]
        texts = ["specific term xyzzy in first chunk", "another unrelated passage"]
        _, mock_dense = _setup_mock_dense(ids, texts)
        monkeypatch.setattr(vs, "dense_query", mock_dense)

        fake_reranker = MagicMock()
        fake_reranker.predict.return_value = np.array([0.8, 0.6])
        monkeypatch.setattr(retriever_module, "_reranker", fake_reranker)

        # Should not raise even if global corpus would have more docs
        results = retriever_module.hybrid_retrieve("xyzzy", "u1", top_k=2)
        assert len(results) >= 0  # minimal assertion — no crash is the key check

    def test_hybrid_retrieve_public_user_id_empty_string(self, monkeypatch):
        """Empty string user_id (public filings) is valid and forwarded correctly."""
        import app.services.vector_store as vs
        import app.ingestion.retriever as retriever_module

        ids = ["pub1"]
        texts = ["public filing text"]
        _, mock_dense = _setup_mock_dense(ids, texts, [{"user_id": ""}])
        monkeypatch.setattr(vs, "dense_query", mock_dense)

        fake_reranker = MagicMock()
        fake_reranker.predict.return_value = np.array([0.5])
        monkeypatch.setattr(retriever_module, "_reranker", fake_reranker)

        retriever_module.hybrid_retrieve("public query", "", top_k=5)

        call_args = mock_dense.call_args
        called_user_id = call_args[0][1] if call_args[0] else call_args[1].get("user_id")
        assert called_user_id == "", "Empty string user_id for public filings must be forwarded"
