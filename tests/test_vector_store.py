"""Tests for app/services/vector_store.py.

All tests mock the ChromaDB collection object at the module level.  No real
ChromaDB connection is made — this satisfies PROJECT.md test boundary (no
real API calls in tests; mock at services/ boundary).

Coverage:
  - test_user_isolation        — INGEST-03: userB query returns zero userA chunks
  - test_none_metadata_rejected — embed_and_store raises on any None metadata value
  - test_dense_query_where_filter — dense_query always forwards where={"user_id": ...}
  - test_canonical_exists_true  — canonical_exists returns True when a PUBLIC chunk is found (CR-01)
  - test_canonical_exists_false — canonical_exists returns False when no chunk found
  - test_canonical_exists_for_user_scopes_to_user_id — canonical_exists_for_user scopes to user_id (CR-01)
  - test_embed_texts_returns_list — embed_texts returns list[list[float]]
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_collection(
    query_ids: list[list[str]] | None = None,
    query_documents: list[list[str]] | None = None,
    query_metadatas: list[list[dict]] | None = None,
    query_distances: list[list[float]] | None = None,
    get_ids: list[str] | None = None,
) -> MagicMock:
    """Return a MagicMock that mimics a ChromaDB Collection."""
    col = MagicMock()
    col.query.return_value = {
        "ids": query_ids or [[]],
        "documents": query_documents or [[]],
        "metadatas": query_metadatas or [[]],
        "distances": query_distances or [[]],
    }
    col.get.return_value = {"ids": get_ids or []}
    return col


def _fake_encode(texts, *, convert_to_numpy=True, **kwargs):
    """Return a numpy-like array of zeros for testing (no model download)."""
    import numpy as np

    return np.zeros((len(texts), 384), dtype="float32")


# ---------------------------------------------------------------------------
# test_user_isolation (INGEST-03)
# ---------------------------------------------------------------------------


def test_user_isolation(monkeypatch):
    """A dense_query for userB must never be able to match userA's private chunks.

    Setup: collection.query called with a userB scope. The mock returns empty
    (ChromaDB enforces the filter server-side); the test's job is to prove the
    filter this function forwards can only ever match userB's own chunks plus
    the public ("") scope — userA (or any other private user) is structurally
    excluded from the filter itself, not just "returns empty in this mock".

    dense_query searches BOTH a user's private scope AND the public scope in
    one call (so a real user's research run can see EDGAR-ingested public
    filings alongside their own uploads) via where={"user_id": {"$in": [...]}}.
    The isolation guarantee is: that $in list contains ONLY userB and ""
    (public) — never another user's id. This test asserts that shape directly,
    which is what makes it a structural proof rather than a mock-returns-empty
    coincidence.
    """
    import app.services.vector_store as vs

    # Patch the module-level embed model so no model is downloaded
    mock_model = MagicMock()
    mock_model.encode.side_effect = _fake_encode
    monkeypatch.setattr(vs, "_embed_model", mock_model)

    # userB query: ChromaDB returns empty (correctly filtered server-side)
    mock_col = _make_mock_collection(
        query_ids=[[]],
        query_documents=[[]],
        query_metadatas=[[]],
        query_distances=[[]],
    )
    monkeypatch.setattr(vs, "vantage_collection", mock_col)

    result = vs.dense_query("revenue growth", user_id="userB", n_results=20)

    # Verify where filter was forwarded to collection.query, and that its
    # scope is EXACTLY {userB, public} -- no other user id can appear here.
    call_kwargs = mock_col.query.call_args.kwargs
    where = call_kwargs["where"]
    assert where == {"user_id": {"$in": ["userB", ""]}}, (
        "dense_query must scope to exactly [user_id, \"\"] (private + public), "
        f"got {where!r}"
    )
    allowed_ids = set(where["user_id"]["$in"])
    assert "userA" not in allowed_ids, (
        "dense_query's filter must never include another user's private id"
    )

    # Result must have no documents for userB (server returns empty, no cross-user leak)
    assert result["ids"][0] == [], "userB query must return zero chunks"
    assert result["documents"][0] == [], "userB query must return zero document texts"


# ---------------------------------------------------------------------------
# test_none_metadata_rejected
# ---------------------------------------------------------------------------


def test_none_metadata_rejected(monkeypatch):
    """embed_and_store must raise ValueError before calling collection.add when
    any metadata dict contains a None value.

    ChromaDB 0.5.x only accepts str/int/float/bool; passing None would raise
    at the ChromaDB level (server-side or client-side).  We guard early so the
    error is clear and no partial add happens.
    """
    import app.services.vector_store as vs

    mock_model = MagicMock()
    mock_model.encode.side_effect = _fake_encode
    monkeypatch.setattr(vs, "_embed_model", mock_model)

    mock_col = MagicMock()
    monkeypatch.setattr(vs, "vantage_collection", mock_col)

    with pytest.raises(ValueError, match="None"):
        vs.embed_and_store(
            ids=["chunk-1"],
            texts=["some text"],
            metadatas=[{"user_id": None, "canonical_id": "abc"}],
        )

    # collection.add must NOT have been called
    mock_col.add.assert_not_called()


# ---------------------------------------------------------------------------
# test_dense_query_where_filter
# ---------------------------------------------------------------------------


def test_dense_query_where_filter(monkeypatch):
    """dense_query must always pass where={"user_id": user_id} to collection.query."""
    import app.services.vector_store as vs

    mock_model = MagicMock()
    mock_model.encode.side_effect = _fake_encode
    monkeypatch.setattr(vs, "_embed_model", mock_model)

    mock_col = _make_mock_collection()
    monkeypatch.setattr(vs, "vantage_collection", mock_col)

    vs.dense_query("operating margin", user_id="", n_results=5)

    mock_col.query.assert_called_once()
    call_kwargs = mock_col.query.call_args.kwargs
    assert "where" in call_kwargs, "dense_query must pass where= to collection.query"
    assert call_kwargs["where"] == {"user_id": ""}, (
        "dense_query must include user_id in where filter (empty string for public)"
    )
    assert call_kwargs["n_results"] == 5


# ---------------------------------------------------------------------------
# test_canonical_exists_true
# ---------------------------------------------------------------------------


def test_canonical_exists_true(monkeypatch):
    """canonical_exists returns True when ChromaDB finds a PUBLIC chunk with that canonical_id.

    CR-01: canonical_exists is scoped to user_id="" (public only) so a private
    user PDF cannot poison the public EDGAR dedup check.
    """
    import app.services.vector_store as vs

    mock_col = _make_mock_collection(get_ids=["chunk-abc"])
    monkeypatch.setattr(vs, "vantage_collection", mock_col)

    result = vs.canonical_exists("sha256abc")

    assert result is True
    mock_col.get.assert_called_once_with(
        where={"$and": [{"canonical_id": "sha256abc"}, {"user_id": ""}]},
        limit=1,
        include=[],
    )


# ---------------------------------------------------------------------------
# test_canonical_exists_for_user (CR-01)
# ---------------------------------------------------------------------------


def test_canonical_exists_for_user_scopes_to_user_id(monkeypatch):
    """canonical_exists_for_user scopes the ChromaDB get() to canonical_id + user_id.

    CR-01: the PDF-upload dedup path uses this function (rather than the
    public-scoped canonical_exists) to detect a user's own re-upload as
    cached, without scanning other users' private chunks or the public scope.
    """
    import app.services.vector_store as vs

    mock_col = _make_mock_collection(get_ids=["chunk-private-1"])
    monkeypatch.setattr(vs, "vantage_collection", mock_col)

    result = vs.canonical_exists_for_user("sha256abc", "user-a-uuid-1234")

    assert result is True
    mock_col.get.assert_called_once_with(
        where={
            "$and": [
                {"canonical_id": "sha256abc"},
                {"user_id": "user-a-uuid-1234"},
            ]
        },
        limit=1,
        include=[],
    )


# ---------------------------------------------------------------------------
# test_canonical_exists_false
# ---------------------------------------------------------------------------


def test_canonical_exists_false(monkeypatch):
    """canonical_exists returns False when ChromaDB finds no matching chunk."""
    import app.services.vector_store as vs

    mock_col = _make_mock_collection(get_ids=[])
    monkeypatch.setattr(vs, "vantage_collection", mock_col)

    result = vs.canonical_exists("sha256xyz")

    assert result is False


# ---------------------------------------------------------------------------
# test_embed_texts_returns_list
# ---------------------------------------------------------------------------


def test_embed_texts_returns_list(monkeypatch):
    """embed_texts returns list[list[float]] with one inner list per input text."""
    import app.services.vector_store as vs

    mock_model = MagicMock()
    mock_model.encode.side_effect = _fake_encode
    monkeypatch.setattr(vs, "_embed_model", mock_model)

    texts = ["Hello world", "SEC filing Q3 2023"]
    result = vs.embed_texts(texts)

    assert isinstance(result, list), "embed_texts must return a list"
    assert len(result) == 2, "one embedding per input text"
    assert isinstance(result[0], list), "each embedding must be a list of floats"
    assert len(result[0]) == 384, "all-MiniLM-L6-v2 produces 384-dim embeddings"
    assert all(isinstance(v, float) for v in result[0]), "embedding values must be float"
