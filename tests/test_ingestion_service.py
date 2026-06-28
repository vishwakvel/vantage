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
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ingestion_service import (
    IngestionResult,
    _ingest_one_filing,
    compute_canonical_id,
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
