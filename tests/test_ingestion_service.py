"""Tests for app/services/ingestion_service.py.

All tests mock at the services/ boundary — no real EDGAR, ChromaDB, or
PostgreSQL calls are made (PROJECT.md test constraint).

Coverage:
  Task 1 — canonical_id + dedup guard:
  - test_canonical_id_dedup            — compute_canonical_id deterministic (INGEST-05)
  - test_dedup_guard_no_edgar_call     — _ingest_one_filing skips EDGAR when cached (INGEST-02)

  Task 2 — ingest_ticker full flow (added in Task 2 RED):
  - test_ingest_ticker_indexes_chunks  — embeds with user_id="" metadata (INGEST-01)
  - test_ingest_ticker_dedup_skips_edgar — second run: 0 EDGAR calls (INGEST-02)
  - test_edgar_failure_returns_warning — failure → source_warnings, no exception (INGEST-04)
  - test_invalid_ticker_rejected       — "../etc" rejected before any EDGAR call

  Plan 05 Task 1 — ingest_pdf private slice (added in RED):
  - test_ingest_pdf_user_scoped        — chunks tagged with uploader's user_id (INGEST-03)
  - test_pdf_failure_returns_warning   — unparseable PDF → source_warning, no exception
  - test_pdf_oversized_rejected        — >50 MB bytes rejected before fitz.open (T-02-03)
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ingestion_service import (
    IngestionResult,
    _ingest_one_filing,
    compute_canonical_id,
    ingest_ticker,
)


# ---------------------------------------------------------------------------
# Task 1: compute_canonical_id — INGEST-05 determinism
# ---------------------------------------------------------------------------


def test_canonical_id_dedup():
    """compute_canonical_id is deterministic and identical across EDGAR and PDF paths.

    INGEST-05: same filing deduplicated via canonical_id from multiple sources.
    The ticker is uppercased before hashing so an EDGAR path (uppercase ticker)
    and a user-PDF upload path (any casing) produce the same canonical_id.
    """
    # EDGAR path passes uppercase ticker
    cid_edgar = compute_canonical_id("AAPL", "10-K", "2023-09-30")
    # PDF-upload path might pass lowercase ticker (user types it)
    cid_pdf = compute_canonical_id("aapl", "10-K", "2023-09-30")

    # Both paths must produce the same canonical_id (INGEST-05)
    assert cid_edgar == cid_pdf

    # Output must be a 64-char lowercase hex string (sha256 hexdigest)
    assert len(cid_edgar) == 64
    assert all(c in "0123456789abcdef" for c in cid_edgar)

    # Value must match the canonical D-09 formula: sha256("{TICKER}:{form}:{period}")
    expected = hashlib.sha256(b"AAPL:10-K:2023-09-30").hexdigest()
    assert cid_edgar == expected

    # Different inputs must produce different canonical_ids
    cid_different_form = compute_canonical_id("AAPL", "10-Q", "2023-06-30")
    assert cid_edgar != cid_different_form

    cid_different_ticker = compute_canonical_id("MSFT", "10-K", "2023-09-30")
    assert cid_edgar != cid_different_ticker


# ---------------------------------------------------------------------------
# Task 1: _ingest_one_filing dedup guard — INGEST-02
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dedup_guard_no_edgar_call():
    """_ingest_one_filing makes no EDGAR call when canonical_exists returns True.

    INGEST-02: previously ingested public docs reused without re-fetching.
    The dedup check must run BEFORE any edgar_client call.
    """
    filing_meta = {
        "form_type": "10-K",
        "period_of_report": "2023-09-30",
        "cik": "0000320193",
        "accession_no": "0000320193-23-000106",
    }
    result = IngestionResult(ticker="AAPL")
    mock_session = AsyncMock()

    with (
        patch("app.services.ingestion_service.canonical_exists", return_value=True),
        patch("app.services.ingestion_service.edgar_client") as mock_edgar,
    ):
        await _ingest_one_filing(filing_meta, "AAPL", mock_session, result)

    # Filing counted as cached — not re-ingested
    assert result.filings_cached == 1
    assert result.filings_ingested == 0
    # No EDGAR calls made at all
    mock_edgar.get.assert_not_called()
    mock_edgar.get_archive.assert_not_called()


# ---------------------------------------------------------------------------
# Task 2: ingest_ticker full flow — INGEST-01, INGEST-02, INGEST-04
# ---------------------------------------------------------------------------

#: Shared EDGAR EFTS search response with one 10-K hit for AAPL
_SEARCH_RESPONSE_DATA = {
    "hits": {
        "hits": [
            {
                "_source": {
                    "form_type": "10-K",
                    "period_of_report": "2023-09-30",
                    "cik": "0000320193",
                    "accession_no": "0000320193-23-000106",
                }
            }
        ]
    }
}

#: EDGAR Archives filing index JSON (points to primary HTML document)
_INDEX_RESPONSE_DATA = {
    "directory": {
        "item": [
            {"name": "0000320193-23-000106-index.htm"},  # index page — must be skipped
            {"name": "aapl-20230930.htm"},                # primary document
        ]
    }
}

#: Minimal 10-K HTML with a recognisable Item 7 (MDA) section
_FILING_HTML = (
    "<html><body>"
    "Item 7 Management Discussion and Analysis "
    "Apple Inc revenues grew thirty percent in fiscal year two thousand twenty three "
    "driven by iPhone and services segments expanding globally."
    "</body></html>"
)


def _make_search_resp() -> MagicMock:
    """Build a mock httpx.Response for the EDGAR EFTS search endpoint."""
    resp = MagicMock()
    resp.json.return_value = _SEARCH_RESPONSE_DATA
    resp.raise_for_status = MagicMock()
    return resp


def _make_index_resp() -> MagicMock:
    """Build a mock httpx.Response for an EDGAR Archives filing index."""
    resp = MagicMock()
    resp.json.return_value = _INDEX_RESPONSE_DATA
    resp.raise_for_status = MagicMock()
    return resp


def _make_html_resp(html: str = _FILING_HTML) -> MagicMock:
    """Build a mock httpx.Response for an EDGAR Archives filing document."""
    resp = MagicMock()
    resp.text = html
    resp.raise_for_status = MagicMock()
    return resp


def _make_session_mock(existing_canonical_ids: list[str] | None = None) -> AsyncMock:
    """Return an AsyncMock session whose execute() returns *existing_canonical_ids*.

    ``add`` is overridden with a plain ``MagicMock`` because ``AsyncSession.add()``
    is synchronous in SQLAlchemy — using AsyncMock would produce unawaited-coroutine
    RuntimeWarnings since the production code calls ``session.add(obj)`` without await.
    """
    mock_session = AsyncMock()
    exec_result = MagicMock()
    rows = [(cid,) for cid in (existing_canonical_ids or [])]
    exec_result.fetchall.return_value = rows
    mock_session.execute.return_value = exec_result
    # add() is synchronous — replace the AsyncMock attribute with a plain MagicMock
    mock_session.add = MagicMock()
    return mock_session


@pytest.mark.anyio
async def test_ingest_ticker_indexes_chunks():
    """ingest_ticker embeds filing chunks with correct metadata (INGEST-01).

    Asserts:
    - embed_and_store is called for a new filing
    - All chunk metadatas carry user_id="" (public filing, INGEST-03 boundary)
    - All chunk metadatas carry ticker="AAPL"
    - IngestionResult.filings_ingested == 1 and source_warnings is empty
    """
    mock_session = _make_session_mock(existing_canonical_ids=[])  # no prior docs

    with (
        patch("app.services.ingestion_service.canonical_exists", return_value=False),
        patch("app.services.ingestion_service.embed_and_store") as mock_embed,
        patch("app.services.ingestion_service.edgar_client") as mock_edgar,
    ):
        mock_edgar.get = AsyncMock(return_value=_make_search_resp())
        mock_edgar.get_archive = AsyncMock(
            side_effect=[_make_index_resp(), _make_html_resp()]
        )

        result = await ingest_ticker("AAPL", mock_session)

    assert result.filings_ingested == 1
    assert result.filings_cached == 0
    assert result.source_warnings == []

    # embed_and_store must have been called exactly once (one filing, one batch)
    mock_embed.assert_called_once()
    call_kwargs = mock_embed.call_args.kwargs
    metadatas = call_kwargs["metadatas"]

    # All chunks must carry user_id="" (public filing, INGEST-03 boundary)
    assert all(m["user_id"] == "" for m in metadatas), (
        "All chunk metadatas must have user_id='' for public filings"
    )
    # All chunks must carry the correct ticker
    assert all(m["ticker"] == "AAPL" for m in metadatas)
    # Section must be a non-empty string from section_constants (not inline literal)
    assert all(isinstance(m["section"], str) and m["section"] for m in metadatas)


@pytest.mark.anyio
async def test_ingest_ticker_dedup_skips_edgar():
    """Second ingest_ticker call makes zero EDGAR calls when filing is cached (INGEST-02).

    Setup: PostgreSQL has an existing EDGAR Document for AAPL; ChromaDB still
    has those chunks (canonical_exists returns True).

    Asserts: edgar_client.get (EFTS search) and get_archive are NOT called.
    The EDGAR network is bypassed entirely on the second run.
    """
    import hashlib as _hashlib

    existing_cid = _hashlib.sha256(b"AAPL:10-K:2023-09-30").hexdigest()
    # Simulate one existing EDGAR Document in PostgreSQL for this ticker
    mock_session = _make_session_mock(existing_canonical_ids=[existing_cid])

    with (
        patch("app.services.ingestion_service.canonical_exists", return_value=True),
        patch("app.services.ingestion_service.embed_and_store") as mock_embed,
        patch("app.services.ingestion_service.edgar_client") as mock_edgar,
    ):
        result = await ingest_ticker("AAPL", mock_session)

    assert result.filings_cached == 1
    assert result.filings_ingested == 0
    assert result.source_warnings == []
    # Zero EDGAR network calls — the pre-flight DB check short-circuits everything
    mock_edgar.get.assert_not_called()
    mock_edgar.get_archive.assert_not_called()
    mock_embed.assert_not_called()


@pytest.mark.anyio
async def test_edgar_failure_returns_warning():
    """EDGAR get_archive failure returns source_warnings, not an exception (INGEST-04).

    The EFTS search succeeds and returns one hit; the Archives download raises.
    ingest_ticker must catch the error and populate source_warnings rather than
    propagating the exception to the caller.
    """
    mock_session = _make_session_mock(existing_canonical_ids=[])

    with (
        patch("app.services.ingestion_service.canonical_exists", return_value=False),
        patch("app.services.ingestion_service.embed_and_store"),
        patch("app.services.ingestion_service.edgar_client") as mock_edgar,
    ):
        mock_edgar.get = AsyncMock(return_value=_make_search_resp())
        # Archive download fails with a network timeout
        mock_edgar.get_archive = AsyncMock(
            side_effect=RuntimeError("EDGAR Archives timeout")
        )

        # Must NOT raise — failures become source_warnings (INGEST-04)
        result = await ingest_ticker("AAPL", mock_session)

    assert result.source_warnings, "source_warnings must be non-empty on EDGAR failure"
    assert result.filings_ingested == 0


@pytest.mark.anyio
async def test_invalid_ticker_rejected():
    """ingest_ticker raises ValueError for invalid tickers before any EDGAR call.

    '../etc' is a path-traversal string that would be dangerous if routed to
    an EDGAR URL.  _validate_ticker must reject it before any network call
    (T-02-02 SSRF mitigation).
    """
    mock_session = AsyncMock()

    with patch("app.services.ingestion_service.edgar_client") as mock_edgar:
        with pytest.raises(ValueError, match="Invalid ticker"):
            await ingest_ticker("../etc", mock_session)

    mock_edgar.get.assert_not_called()
    mock_edgar.get_archive.assert_not_called()


# ---------------------------------------------------------------------------
# Plan 05 Task 1 RED: ingest_pdf — user-scoped private PDF slice
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ingest_pdf_user_scoped():
    """ingest_pdf stores chunks tagged with the uploader's user_id (INGEST-03).

    Asserts:
    - All chunk metadatas carry user_id equal to the uploader's uuid (non-empty)
    - Chunk IDs include the user_id for per-user namespacing
    - Chunk IDs include the canonical_id (same formula as ingest_ticker — INGEST-05)
    - IngestionResult.filings_ingested == 1 and source_warnings is empty
    - fitz.open is called (text extraction happened)
    """
    from app.services.ingestion_service import ingest_pdf  # fails in RED: function not yet defined

    user_id = "user-a-uuid-1234"
    mock_session = _make_session_mock(existing_canonical_ids=[])

    # Build a mock fitz document: one page returning SEC-like text with an Item 7 section
    mock_page = MagicMock()
    mock_page.get_text.return_value = (
        "Item 7 Management Discussion revenues grew thirty percent "
        "fiscal year two thousand twenty three driven by iPhone services expanding."
    )
    mock_doc = MagicMock()
    mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))

    with (
        patch("app.services.ingestion_service.canonical_exists", return_value=False),
        patch("app.services.ingestion_service.embed_and_store") as mock_embed,
        patch("app.services.ingestion_service.fitz") as mock_fitz,
    ):
        mock_fitz.open.return_value = mock_doc
        result = await ingest_pdf(
            file_bytes=b"fake-pdf-content",
            user_id=user_id,
            ticker="AAPL",
            form_type="10-K",
            period_of_report="2023-09-30",
            session=mock_session,
        )

    assert result.filings_ingested == 1
    assert result.source_warnings == []

    # embed_and_store must have been called exactly once (one filing, one batch)
    mock_embed.assert_called_once()
    call_kwargs = mock_embed.call_args.kwargs
    metadatas = call_kwargs["metadatas"]
    ids = call_kwargs["ids"]

    # All chunks must carry the uploader's user_id (INGEST-03 boundary)
    assert all(m["user_id"] == user_id for m in metadatas), (
        "Private chunks must be tagged with the uploader's user_id"
    )
    # user_id must be non-empty (never empty string like public filings)
    assert all(m["user_id"] != "" for m in metadatas)

    # Chunk IDs must include user_id for per-user namespacing
    assert all(user_id in chunk_id for chunk_id in ids), (
        "Chunk IDs must contain user_id for per-user namespacing"
    )
    # Chunk IDs must also include canonical_id — same formula as ingest_ticker (INGEST-05)
    expected_canonical = compute_canonical_id("AAPL", "10-K", "2023-09-30")
    assert all(expected_canonical in chunk_id for chunk_id in ids), (
        "Chunk IDs must embed canonical_id to share dedup key with EDGAR path"
    )

    # fitz.open must have been called (text extraction path exercised)
    mock_fitz.open.assert_called_once()


@pytest.mark.anyio
async def test_pdf_failure_returns_warning():
    """An unparseable PDF returns IngestionResult with source_warnings; no exception raised.

    Simulates fitz.open raising on corrupt/invalid PDF bytes.
    ingest_pdf must catch the parse error and return normally (INGEST-04 pattern).
    """
    from app.services.ingestion_service import ingest_pdf  # fails in RED: function not yet defined

    mock_session = _make_session_mock()

    with (
        patch("app.services.ingestion_service.canonical_exists", return_value=False),
        patch("app.services.ingestion_service.embed_and_store") as mock_embed,
        patch("app.services.ingestion_service.fitz") as mock_fitz,
    ):
        mock_fitz.open.side_effect = Exception("Corrupt PDF stream")

        # Must NOT raise — parse failures become source_warnings (INGEST-04)
        result = await ingest_pdf(
            file_bytes=b"not-a-real-pdf",
            user_id="user-a-uuid-1234",
            ticker="AAPL",
            form_type="10-K",
            period_of_report="2023-09-30",
            session=mock_session,
        )

    assert result.source_warnings, "source_warnings must be non-empty on PDF parse failure"
    assert result.filings_ingested == 0
    mock_embed.assert_not_called()


@pytest.mark.anyio
async def test_pdf_oversized_rejected():
    """PDF bytes over 50 MB are rejected before fitz.open is called (T-02-03 DoS guard).

    ingest_pdf must check len(file_bytes) > 50 MB BEFORE calling fitz.open to avoid
    parsing an adversarially large file (T-02-03).
    """
    from app.services.ingestion_service import ingest_pdf  # fails in RED: function not yet defined

    mock_session = _make_session_mock()
    # 50 MB + 1 byte — just over the threshold
    oversized_bytes = b"x" * (50 * 1024 * 1024 + 1)

    with (
        patch("app.services.ingestion_service.fitz") as mock_fitz,
        patch("app.services.ingestion_service.embed_and_store") as mock_embed,
    ):
        result = await ingest_pdf(
            file_bytes=oversized_bytes,
            user_id="user-a-uuid-1234",
            ticker="AAPL",
            form_type="10-K",
            period_of_report="2023-09-30",
            session=mock_session,
        )

    # fitz.open must NOT be called for oversized files (reject before parse)
    mock_fitz.open.assert_not_called()
    mock_embed.assert_not_called()
    assert result.source_warnings, "Oversized PDF must produce a source_warning"
