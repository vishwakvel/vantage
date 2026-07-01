"""Vector store singleton — ChromaDB HTTP client + sentence-transformers embeddings.

Wraps ChromaDB 0.5.x (HTTP mode) and the sentence-transformers all-MiniLM-L6-v2
model into a single module-level singleton following the edgar_client pattern.

Design principles (from 02-RESEARCH.md):
  - Module-level singletons: chroma_client, vantage_collection, _embed_model
  - All three are lazy (None at import time) and populated on first use.  This
    avoids blocking import with ChromaDB connection attempts or ~80 MB model
    downloads, and lets tests monkeypatch the singletons before any call.
  - Every dense_query call MUST include where={"user_id": user_id} — this is the
    structural enforcement boundary for INGEST-03 (private-doc isolation)
  - None metadata values are rejected before calling collection.add — ChromaDB
    0.5.x raises ValueError on None; guarding early produces a clear error message
  - No Groq API calls anywhere in this module

Public API::

    from app.services.vector_store import embed_texts, embed_and_store, dense_query, canonical_exists

Module-level singletons (monkeypatchable in tests)::

    chroma_client       — chromadb.HttpClient (lazy)
    vantage_collection  — chromadb Collection (lazy)
    _embed_model        — SentenceTransformer (lazy)
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Module-level singletons — all lazy (None at import time)
# ---------------------------------------------------------------------------

chroma_client: Any = None
vantage_collection: Any = None
_embed_model: Any = None


# ---------------------------------------------------------------------------
# Lazy initialisation helpers
# ---------------------------------------------------------------------------


def _get_chroma_collection() -> Any:
    """Return the module-level ChromaDB collection, creating it on first call.

    Lazy init avoids a connection attempt at import time so that:
      - Tests can monkeypatch ``vantage_collection`` before calling any function.
      - App startup does not fail if ChromaDB is temporarily unavailable.
    """
    global chroma_client, vantage_collection  # noqa: PLW0603
    if vantage_collection is None:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        from app.core.config import get_settings

        _settings = get_settings()
        chroma_client = chromadb.HttpClient(
            host=_settings.CHROMADB_HOST,
            port=_settings.CHROMADB_PORT,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        vantage_collection = chroma_client.get_or_create_collection(
            name=_settings.CHROMADB_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
    return vantage_collection


def _get_embed_model() -> Any:
    """Return the module-level SentenceTransformer, constructing it on first call.

    Lazy init avoids blocking import with a ~80 MB model download.  The model
    is cached under ~/.cache/huggingface after the first download.
    """
    global _embed_model  # noqa: PLW0603
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer

        _embed_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    return _embed_model


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of text strings using all-MiniLM-L6-v2.

    Args:
        texts: Non-empty list of strings to embed.  Each string should be
               within the 256-token model limit (~250 words for financial text).

    Returns:
        list[list[float]] — one 384-dimensional embedding per input string.
    """
    model = _get_embed_model()
    embeddings = model.encode(
        texts,
        batch_size=32,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return [emb.tolist() for emb in embeddings]


def embed_and_store(
    ids: list[str],
    texts: list[str],
    metadatas: list[dict[str, Any]],
) -> None:
    """Embed texts and add them to the ChromaDB collection.

    Rejects metadata dicts containing any None value before calling collection.add
    — ChromaDB 0.5.x only accepts str/int/float/bool metadata values.

    Args:
        ids:       Unique chunk IDs (must not already exist in the collection
                   unless using collection.upsert — this function uses .add).
        texts:     Raw chunk text (same length as ids).
        metadatas: Per-chunk metadata dicts (same length as ids).
                   Use "" (empty string) for absent user_id on public filings;
                   never pass None.

    Raises:
        ValueError: If any metadata dict contains a None value.
    """
    # Guard: reject None metadata values before any network call (INGEST-03, Pitfall 2)
    for i, meta in enumerate(metadatas):
        for key, value in meta.items():
            if value is None:
                raise ValueError(
                    f"Metadata value None is not supported by ChromaDB 0.5.x. "
                    f"chunk index={i}, key='{key}'. Use '' for absent user_id."
                )

    collection = _get_chroma_collection()
    embeddings = embed_texts(texts)
    collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=texts,
        metadatas=metadatas,
    )


def dense_query(
    query_text: str,
    user_id: str,
    n_results: int = 20,
) -> dict[str, Any]:
    """Query ChromaDB for the nearest neighbours to query_text, scoped to user_id.

    INGEST-03 enforcement: every query path MUST include where={"user_id": user_id}.
    Public filings use user_id="" (empty string); private docs use the user's UUID
    string.  The ChromaDB server enforces the filter; this function's job is to
    ensure the filter is ALWAYS forwarded — no call to collection.query exists in
    this module without it.

    Args:
        query_text: Free-text query to embed and search.
        user_id:    Scope filter — "" for public-only, user UUID str for private.
        n_results:  Number of nearest neighbours to return (default 20).

    Returns:
        dict with keys "ids", "documents", "metadatas", "distances".
        Each value is a list-of-lists (outer = one per query embedding).
    """
    collection = _get_chroma_collection()
    query_embedding = embed_texts([query_text])
    results = collection.query(
        query_embeddings=query_embedding,
        n_results=n_results,
        where={"user_id": user_id},
        include=["documents", "metadatas", "distances"],
    )
    return results  # type: ignore[return-value]


def canonical_exists(canonical_id: str) -> bool:
    """Check whether a PUBLIC chunk with the given canonical_id already exists.

    Scoped to user_id="" so that a private user PDF does not poison the public
    EDGAR dedup check (CR-01): otherwise a private upload sharing the same
    canonical_id would permanently block the public filing from ever being
    indexed, since the public-facing dedup check would see the private chunk
    and treat the filing as already cached.

    Used for dedup before embedding — if the filing is already indexed, skip
    the embed/store step (INGEST-02).

    Args:
        canonical_id: sha256hex canonical identifier for a filing.

    Returns:
        True if at least one PUBLIC chunk with canonical_id exists, False otherwise.
    """
    collection = _get_chroma_collection()
    result = collection.get(
        where={
            "$and": [
                {"canonical_id": canonical_id},
                {"user_id": ""},  # public scope only (CR-01)
            ]
        },
        limit=1,
        include=[],
    )
    return len(result["ids"]) > 0


def canonical_exists_for_user(canonical_id: str, user_id: str) -> bool:
    """Check whether *user_id* already has a private chunk with this canonical_id.

    Used by the PDF-upload dedup path so a user re-uploading the same filing
    is detected as cached, without scanning (or being poisoned by) other
    users' private chunks or the public EDGAR chunk set (CR-01).

    Args:
        canonical_id: sha256hex canonical identifier for a filing.
        user_id:      UUID string of the uploading user (never "" — use
                      canonical_exists() for the public scope instead).

    Returns:
        True if at least one chunk with canonical_id exists for user_id.
    """
    collection = _get_chroma_collection()
    result = collection.get(
        where={
            "$and": [
                {"canonical_id": canonical_id},
                {"user_id": user_id},
            ]
        },
        limit=1,
        include=[],
    )
    return len(result["ids"]) > 0
