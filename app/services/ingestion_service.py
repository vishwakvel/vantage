"""Ingestion service — SEC public filing slice end-to-end.

``ingest_ticker(ticker, session)`` resolves recent 10-K/10-Q filings via
EDGAR, deduplicates by canonical_id, chunks, embeds, and stores into
ChromaDB + PostgreSQL, returning a non-fatal ``IngestionResult``.

Design principles (02-RESEARCH.md / PLAN 02-03):
  - Ticker validation: uppercase, 1-10 alphanumerics, BEFORE any EDGAR call
    → mitigates SSRF (T-02-02)
  - compute_canonical_id: sha256(f"{TICKER}:{form_type}:{period_of_report}")
    → deterministic across EDGAR and user-PDF paths (D-09, INGEST-05)
  - Pre-flight PostgreSQL check: if all canonical_ids for this ticker already
    exist in ChromaDB, return without calling EDGAR at all (INGEST-02)
  - canonical_exists() dedup check BEFORE any EDGAR fetch or embed per filing
    (INGEST-02)
  - Source failures → source_warnings, never propagate as 500 (INGEST-04)
  - asyncio.sleep(0.1) between sequential filing fetches (Pitfall 3, EDGAR
    rate limiting)
  - Public filings: user_id="" in ChromaDB metadata; Document.user_id=None
    in PostgreSQL (INGEST-03 boundary)
  - All EDGAR access through ``edgar_client`` only — direct HTTP calls are
    prohibited throughout the codebase

Public API::

    from app.services.ingestion_service import ingest_ticker, IngestionResult

Private helpers (importable for testing)::

    compute_canonical_id, _ingest_one_filing
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentChunk, DocumentSourceType, DocumentVisibility
from app.ingestion.chunker import section_aware_chunk
from app.services.edgar_client import edgar_client
from app.services.vector_store import canonical_exists, embed_and_store

# ---------------------------------------------------------------------------
# Ticker validation
# ---------------------------------------------------------------------------

#: Compiled pattern: 1-10 uppercase alphanumeric characters only.
#: Guards against SSRF by rejecting path-traversal strings like "../etc".
_TICKER_RE: re.Pattern[str] = re.compile(r"^[A-Z0-9]{1,10}$")


def _validate_ticker(ticker: str) -> str:
    """Uppercase and validate *ticker*.

    Raises:
        ValueError: If the ticker contains characters outside [A-Z0-9] or
                    is longer than 10 characters (T-02-02 SSRF mitigation).

    Returns:
        Uppercase ticker string.
    """
    normalized = ticker.upper()
    if not _TICKER_RE.match(normalized):
        raise ValueError(
            f"Invalid ticker {ticker!r}. Must be 1-10 uppercase alphanumeric characters."
        )
    return normalized


# ---------------------------------------------------------------------------
# IngestionResult
# ---------------------------------------------------------------------------


@dataclass
class IngestionResult:
    """Outcome of a single ``ingest_ticker()`` invocation.

    Attributes:
        ticker:           Validated, uppercased ticker symbol.
        filings_ingested: Number of filings newly embedded in this call.
        filings_cached:   Number of filings skipped (canonical_id already
                          in ChromaDB — INGEST-02).
        source_warnings:  Non-fatal per-filing error messages (INGEST-04).
                          The caller receives an IngestionResult even when
                          individual filing fetches fail.
    """

    ticker: str
    filings_ingested: int = 0
    filings_cached: int = 0
    source_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# compute_canonical_id  (D-09, INGEST-05)
# ---------------------------------------------------------------------------


def compute_canonical_id(ticker: str, form_type: str, period_of_report: str) -> str:
    """Return sha256 hexdigest of ``'{TICKER}:{form_type}:{period_of_report}'``.

    The ticker is uppercased before hashing so an EDGAR-path caller (which
    receives the ticker in uppercase from EFTS) and a user-PDF-upload caller
    (who may type any casing) produce **identical** canonical_ids for the same
    filing (D-09, INGEST-05).

    Args:
        ticker:           Ticker symbol (case-insensitive; uppercased here).
        form_type:        SEC form type, e.g. ``"10-K"`` or ``"10-Q"``.
        period_of_report: ISO date string, e.g. ``"2023-09-30"``.

    Returns:
        64-character lowercase hex string (sha256 hexdigest).
    """
    raw = f"{ticker.upper()}:{form_type}:{period_of_report}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_primary_doc(index_data: dict[str, Any]) -> str | None:
    """Extract the primary HTML/text filename from an EDGAR filing index JSON.

    EDGAR Archives filing index JSON structure::

        {
          "directory": {
            "item": [
              {"name": "{accession}-index.htm", ...},
              {"name": "aapl-20230930.htm",      ...}
            ]
          }
        }

    Skips the ``*-index.htm`` self-referential index page.  Returns the first
    ``.htm`` / ``.html`` / ``.txt`` document filename found.

    Args:
        index_data: Parsed JSON from ``{accession_clean}-index.json``.

    Returns:
        Document filename string, or ``None`` if none is found.
    """
    try:
        items = index_data.get("directory", {}).get("item", [])
        for item in items:
            name = str(item.get("name", ""))
            if name.lower().endswith(("-index.htm", "-index.html")):
                continue  # skip the filing index page itself
            if name.lower().endswith((".htm", ".html", ".txt")):
                return name
    except (AttributeError, TypeError):
        pass
    return None


async def _ingest_one_filing(
    filing_meta: dict[str, Any],
    ticker: str,
    session: AsyncSession,
    result: IngestionResult,
) -> None:
    """Ingest one EDGAR filing: dedup → download → chunk → embed → persist.

    The dedup check (``canonical_exists``) runs **before** any EDGAR network
    call or embedding work.  On a cache hit the function returns early and
    increments ``result.filings_cached``.  On a miss it downloads the filing
    from Archives, chunks it, embeds into ChromaDB, and persists
    ``Document`` + ``DocumentChunk`` rows to PostgreSQL.

    This function does **not** catch exceptions — ``ingest_ticker`` wraps each
    call in try/except and converts failures to ``source_warnings`` (INGEST-04).

    Args:
        filing_meta: Dict from an EDGAR EFTS ``_source`` block.  Expected keys:
                     ``form_type``, ``period_of_report``, ``cik``,
                     ``accession_no``.
        ticker:      Already-validated uppercase ticker string.
        session:     SQLAlchemy async session for PostgreSQL writes.
        result:      ``IngestionResult`` accumulator — mutated in place.
    """
    form_type = filing_meta.get("form_type", "")
    period_of_report = filing_meta.get("period_of_report", "")
    cik = filing_meta.get("cik", "")
    accession_no = filing_meta.get("accession_no", "")

    canonical_id = compute_canonical_id(ticker, form_type, period_of_report)

    # Dedup check — BEFORE any EDGAR fetch or embed (INGEST-02)
    if canonical_exists(canonical_id):
        result.filings_cached += 1
        return

    # --- Non-cached path: download filing from SEC Archives ---

    # Dashes stripped per EDGAR Archives URL convention
    # e.g. "0000320193-23-000106" → "000032019323000106"
    accession_clean = accession_no.replace("-", "")
    index_path = (
        f"/Archives/edgar/data/{cik}/{accession_clean}/"
        f"{accession_clean}-index.json"
    )
    index_resp = await edgar_client.get_archive(index_path)
    index_resp.raise_for_status()
    index_data = index_resp.json()

    primary_doc = _find_primary_doc(index_data)
    if not primary_doc:
        result.source_warnings.append(
            f"No primary document found for {ticker} {form_type} {period_of_report}"
            f" (accession {accession_no})"
        )
        return

    doc_path = f"/Archives/edgar/data/{cik}/{accession_clean}/{primary_doc}"
    doc_resp = await edgar_client.get_archive(doc_path)
    doc_resp.raise_for_status()
    html = doc_resp.text

    # --- Chunk the filing HTML ---

    # user_id="" for public filings — never None (ChromaDB 0.5.x Pitfall 2)
    base_metadata: dict[str, Any] = {
        "canonical_id": canonical_id,
        "ticker": ticker,
        "form_type": form_type,
        "period_of_report": period_of_report,
        "user_id": "",
    }
    chunks = section_aware_chunk(html, base_metadata)
    if not chunks:
        result.source_warnings.append(
            f"No chunks produced for {ticker} {form_type} {period_of_report}"
        )
        return

    # --- Embed and store in ChromaDB ---

    ids = [f"{canonical_id}:{chunk['metadata']['chunk_index']}" for chunk in chunks]
    texts = [chunk["text"] for chunk in chunks]
    metadatas = [chunk["metadata"] for chunk in chunks]
    embed_and_store(ids=ids, texts=texts, metadatas=metadatas)

    # --- Persist Document + DocumentChunk rows to PostgreSQL ---

    doc_url = f"https://www.sec.gov{doc_path}"
    doc = Document(
        canonical_id=canonical_id,
        user_id=None,  # public filing — user_id NULL in PostgreSQL
        ticker=ticker,
        source_type=DocumentSourceType.EDGAR,
        visibility=DocumentVisibility.PUBLIC,
        title=f"{ticker} {form_type} {period_of_report}",
        url=doc_url,
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(doc)
    await session.flush()  # populate doc.id from PostgreSQL gen_random_uuid()

    for text, meta, chunk_id in zip(texts, metadatas, ids):
        doc_chunk = DocumentChunk(
            document_id=doc.id,
            ticker=ticker,
            section=meta["section"],
            chunk_index=meta["chunk_index"],
            content=text,
            embedding_id=chunk_id,
        )
        session.add(doc_chunk)

    await session.commit()
    result.filings_ingested += 1


# ---------------------------------------------------------------------------
# Public API — ingest_ticker
# ---------------------------------------------------------------------------


async def ingest_ticker(ticker: str, session: AsyncSession) -> IngestionResult:
    """Ingest recent 10-K/10-Q filings for *ticker* into ChromaDB + PostgreSQL.

    Flow:
      1. Validate + uppercase the ticker (T-02-02 SSRF guard).
      2. Pre-flight: query PostgreSQL for existing EDGAR ``Document`` rows for
         this ticker.  If all their canonical_ids are still in ChromaDB,
         return immediately — **zero EDGAR calls on second run** (INGEST-02).
      3. EDGAR EFTS search: last 4 filings (D-01, D-02).
      4. For each filing: ``_ingest_one_filing`` — dedup guard → download →
         chunk → embed → persist.  Wrap each in try/except; failures become
         ``source_warnings`` (INGEST-04).
      5. Sleep 100 ms between sequential filing fetches (Pitfall 3).

    Args:
        ticker:  Company ticker symbol (case-insensitive).
        session: SQLAlchemy async session for PostgreSQL writes.

    Returns:
        ``IngestionResult`` — never raises; failures surface as
        ``source_warnings``.

    Raises:
        ValueError: If *ticker* contains invalid characters (T-02-02).
    """
    ticker = _validate_ticker(ticker)
    result = IngestionResult(ticker=ticker)

    # --- Pre-flight: check for existing EDGAR docs in PostgreSQL ---
    # If all canonical_ids for this ticker are already in ChromaDB, skip EDGAR
    # entirely on this run (INGEST-02: zero EDGAR calls on second run).
    existing_rows = await session.execute(
        select(Document.canonical_id).where(
            Document.ticker == ticker,
            Document.source_type == DocumentSourceType.EDGAR,
        )
    )
    existing_canonical_ids = [row[0] for row in existing_rows.fetchall()]

    if existing_canonical_ids and all(
        canonical_exists(cid) for cid in existing_canonical_ids
    ):
        result.filings_cached = len(existing_canonical_ids)
        return result

    # --- EDGAR EFTS search for recent 10-K/10-Q filings ---

    three_years_ago = (
        datetime.now(timezone.utc) - timedelta(days=365 * 3)
    ).strftime("%Y-%m-%d")

    try:
        search_resp = await edgar_client.get(
            "/LATEST/search-index",
            params={
                "q": f'"{ticker}"',
                "forms": "10-K,10-Q",
                "dateRange": "custom",
                "startdt": three_years_ago,
            },
        )
        search_resp.raise_for_status()
        data = search_resp.json()
        hits = data.get("hits", {}).get("hits", [])[:4]  # D-02: last 4 filings
    except Exception as exc:
        result.source_warnings.append(
            f"EDGAR search failed for {ticker}: {exc}"
        )
        return result

    # --- Process each filing ---

    for i, hit in enumerate(hits):
        source = hit.get("_source", {})
        filing_meta = {
            "form_type": source.get("form_type", ""),
            "period_of_report": source.get("period_of_report", ""),
            "cik": source.get("cik", ""),
            "accession_no": source.get("accession_no", ""),
        }
        try:
            await _ingest_one_filing(filing_meta, ticker, session, result)
        except Exception as exc:
            result.source_warnings.append(
                f"Failed to ingest {ticker} "
                f"{filing_meta.get('form_type')} "
                f"{filing_meta.get('period_of_report')}: {exc}"
            )

        # Rate-limit: sleep between sequential filing fetches (Pitfall 3)
        if i < len(hits) - 1:
            await asyncio.sleep(0.1)

    return result
