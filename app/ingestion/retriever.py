"""Hybrid retriever — dense (ChromaDB) + sparse (BM25) fused by RRF, then re-scored
by a local cross-encoder.

Public API::

    hybrid_retrieve(query, user_id, top_k=10) -> list[dict]
    reciprocal_rank_fusion(dense_ids, sparse_ids, k=60) -> list[str]
    bm25_rank(query, candidate_texts) -> list[int]

Design principles (from 02-PLAN.md / 02-RESEARCH.md):
  - BM25 is built ONLY from the dense candidate set returned by ChromaDB
    (Pitfall 5: never build BM25 from the full collection).
  - user_id is a required argument to hybrid_retrieve and is ALWAYS forwarded
    to vector_store.dense_query — structural enforcement of INGEST-03.
  - The CrossEncoder reranker singleton (_reranker) is lazy (None at import
    time) so tests can monkeypatch it before any call; no model download on import.
  - Tokenisation for BM25 is lower().split() per D-03 / RT-5.

Threat model:
  - T-02-01 (Information Disclosure): user_id is never optional; tests assert
    it is forwarded to dense_query on every call.
  - T-02-06 (DoS - BM25 build size): BM25 built over at most n_results=20
    candidates, not the entire ChromaDB collection.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from rank_bm25 import BM25Okapi

import app.services.vector_store as vector_store

# ---------------------------------------------------------------------------
# Module-level reranker singleton — lazy (None at import time)
# Tests monkeypatch this attribute before calling hybrid_retrieve.
# ---------------------------------------------------------------------------

_reranker: Any = None


def _get_reranker() -> Any:
    """Return the module-level CrossEncoder, constructing it on first call.

    Lazy init avoids blocking import with a model download. The model
    (cross-encoder/ms-marco-MiniLM-L-6-v2) is cached in ~/.cache/huggingface
    after the first download (~85 MB).
    """
    global _reranker  # noqa: PLW0603
    if _reranker is None:
        from sentence_transformers import CrossEncoder

        _reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
    return _reranker


# ---------------------------------------------------------------------------
# Pure ranking functions
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion(
    dense_ids: list[str],
    sparse_ids: list[str],
    k: int = 60,
) -> list[str]:
    """Merge two ranked lists using Reciprocal Rank Fusion (RRF, k=60).

    Formula: score(d) = sum_i 1 / (k + rank_i(d))
    where rank_i(d) is the 1-based position of document d in list i.

    An id appearing highly in both lists outranks an id appearing highly in
    only one list (because it accumulates two positive contributions).

    Args:
        dense_ids:  IDs from ChromaDB dense retrieval, best-first (rank 1 = index 0).
        sparse_ids: IDs from BM25 sparse retrieval, best-first (rank 1 = index 0).
        k:          RRF damping constant (default 60 per D-04).

    Returns:
        All unique ids from both lists, sorted by descending RRF score.
    """
    scores: dict[str, float] = {}

    for rank, doc_id in enumerate(dense_ids, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)

    for rank, doc_id in enumerate(sparse_ids, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)

    return sorted(scores, key=lambda x: scores[x], reverse=True)


def bm25_rank(query: str, candidate_texts: list[str]) -> list[int]:
    """Score candidate texts against a query using BM25Okapi and return sorted indices.

    BM25 is built ONLY from candidate_texts (the dense-retrieved candidate set),
    never from the entire ChromaDB collection (Pitfall 5).

    Tokenisation: lower().split() — matches RT-5 recommendation.

    Args:
        query:           Free-text query string.
        candidate_texts: Candidate passage texts (the dense candidate set).

    Returns:
        List of indices into candidate_texts sorted by descending BM25 score
        (index 0 = best-matching candidate).
    """
    if not candidate_texts:
        return []

    tokenized_corpus = [text.lower().split() for text in candidate_texts]
    bm25 = BM25Okapi(tokenized_corpus)

    tokenized_query = query.lower().split()
    scores = bm25.get_scores(tokenized_query)  # numpy array, len == len(corpus)

    # argsort gives ascending; [::-1] reverses to descending
    sorted_indices = np.argsort(scores)[::-1].tolist()
    return sorted_indices


# ---------------------------------------------------------------------------
# Hybrid retrieval — orchestrates dense + sparse + RRF + cross-encoder
# ---------------------------------------------------------------------------


def hybrid_retrieve(
    query: str,
    user_id: str,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Retrieve the top_k most relevant chunks for query, scoped to user_id.

    Pipeline:
      1. Dense retrieval: vector_store.dense_query(query, user_id, n_results=20)
         — always includes user_id filter (INGEST-03 enforcement).
      2. Sparse ranking: bm25_rank(query, dense candidate texts)
         — BM25 is built only from the dense candidate set (Pitfall 5).
      3. Fusion: reciprocal_rank_fusion(dense_ids, bm25_ordered_ids, k=60)
         — produces a merged ranking.
      4. Re-ranking: CrossEncoder.predict([(query, text)] for fused top-N)
         — re-scores with a cross-encoder; sort descending; return top_k.

    Args:
        query:   Free-text query string.
        user_id: Scope filter — "" for public-only, user UUID str for private docs.
                 This is a REQUIRED argument; retrieval without a user_id scope
                 violates INGEST-03 (private-doc isolation).
        top_k:   Maximum number of results to return (default 10).

    Returns:
        list of dicts, each with keys:
          - "id":       chunk id (str)
          - "text":     chunk text (str)
          - "metadata": chunk metadata dict
          - "score":    cross-encoder relevance score (float, higher = more relevant)
        Ordered by descending score. Length is min(len(candidates), top_k).
    """
    # ------------------------------------------------------------------
    # Step 1: Dense retrieval (always carries user_id — INGEST-03)
    # ------------------------------------------------------------------
    dense_results = vector_store.dense_query(query, user_id, n_results=20)

    ids: list[str] = dense_results["ids"][0]
    texts: list[str] = dense_results["documents"][0]
    metadatas: list[dict] = dense_results["metadatas"][0]

    if not ids:
        return []

    # ------------------------------------------------------------------
    # Step 2: BM25 sparse ranking over the dense candidate set (Pitfall 5)
    # ------------------------------------------------------------------
    bm25_order = bm25_rank(query, texts)
    # Map BM25 rank-ordered indices back to ids (to produce a list of ids ordered by BM25 score)
    bm25_ordered_ids = [ids[i] for i in bm25_order]

    # ------------------------------------------------------------------
    # Step 3: RRF fusion — merge dense ids with BM25-ordered ids
    # ------------------------------------------------------------------
    fused_ids = reciprocal_rank_fusion(ids, bm25_ordered_ids, k=60)

    # ------------------------------------------------------------------
    # Step 4: Cross-encoder re-ranking
    # ------------------------------------------------------------------
    # Build a lookup from id → (text, metadata)
    id_to_data: dict[str, tuple[str, dict]] = {
        doc_id: (text, meta)
        for doc_id, text, meta in zip(ids, texts, metadatas)
    }

    # Keep only ids that exist in our candidate pool (union may not add new ids
    # but defensive guard is cheap)
    fused_candidates = [doc_id for doc_id in fused_ids if doc_id in id_to_data]

    if not fused_candidates:
        return []

    pairs = [(query, id_to_data[doc_id][0]) for doc_id in fused_candidates]
    reranker = _get_reranker()
    scores = reranker.predict(pairs)  # numpy array of floats

    # Sort by descending cross-encoder score
    scored = sorted(
        zip(fused_candidates, scores),
        key=lambda x: x[1],
        reverse=True,
    )

    results = []
    for doc_id, score in scored[:top_k]:
        text, metadata = id_to_data[doc_id]
        results.append(
            {
                "id": doc_id,
                "text": text,
                "metadata": metadata,
                "score": float(score),
            }
        )

    return results
