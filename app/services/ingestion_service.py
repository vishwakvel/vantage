"""Ingestion service — SEC public filing slice + private PDF slice.

``ingest_ticker(ticker, session)`` resolves recent 10-K/10-Q filings via
EDGAR, deduplicates by canonical_id, chunks, embeds, and stores into
ChromaDB + PostgreSQL, returning a non-fatal ``IngestionResult``.

``ingest_pdf(file_bytes, user_id, ticker, form_type, period_of_report, session)``
extracts text from a user-uploaded PDF via PyMuPDF, chunks it through the same
pipeline, and stores chunks tagged with the uploading user's ``user_id``.

Design principles (02-RESEARCH.md / PLAN 02-03, 02-05):
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
  - Private PDFs: user_id=<uuid> in ChromaDB metadata; Document.user_id=uuid
    in PostgreSQL (INGEST-03 boundary)
  - PDF bytes > 50 MB rejected before fitz.open (T-02-03 DoS mitigation)
  - All EDGAR access through ``edgar_client`` only — direct HTTP calls are
    prohibited throughout the codebase

Public API::

    from app.services.ingestion_service import ingest_ticker, ingest_pdf, IngestionResult

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

import fitz  # PyMuPDF — lazy at call time; module-level import enables monkeypatching in tests
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentChunk, DocumentSourceType, DocumentVisibility
from app.ingestion.chunker import section_aware_chunk
from app.services.edgar_client import edgar_client
from app.services.vector_store import (
    canonical_exists,
    canonical_exists_for_user,
    embed_and_store,
)

# ---------------------------------------------------------------------------
# PDF ingestion constants
# ---------------------------------------------------------------------------

#: Maximum bytes accepted before parse; guards against adversarial large PDFs (T-02-03).
_MAX_PDF_BYTES: int = 50 * 1024 * 1024  # 50 MB

# ---------------------------------------------------------------------------
# WR-02: offload blocking vector_store calls to a thread-pool executor
# ---------------------------------------------------------------------------


async def _run_sync(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous, blocking callable off the event loop (WR-02).

    ``canonical_exists``, ``canonical_exists_for_user``, and ``embed_and_store``
    in ``vector_store`` are synchronous — they call ``SentenceTransformer.encode``
    (CPU-bound) and blocking ChromaDB HTTP calls. Calling them directly from
    ``ingest_ticker``/``ingest_pdf`` (both ``async def``) blocks the event loop
    for the duration of the call, stalling every other concurrent request.
    Running them via ``loop.run_in_executor`` keeps the server responsive.

    The public ``vector_store`` API intentionally stays synchronous (it is
    exercised directly and synchronously by ``tests/test_vector_store.py``);
    this wrapper isolates the async offload to the actual async call sites.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


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
# WR-04: form_type / period_of_report validation (PDF upload path)
# ---------------------------------------------------------------------------

#: SEC form types accepted by this pipeline — mirrors the "10-K,10-Q" filter
#: already used in the EDGAR EFTS search (ingest_ticker).
_FORM_TYPE_RE: re.Pattern[str] = re.compile(r"^10-[KQ]$")

#: ISO date (YYYY-MM-DD) — the format EDGAR reports period_of_report in.
_PERIOD_RE: re.Pattern[str] = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_form_type(form_type: str) -> str:
    """Validate *form_type* against the accepted SEC form types.

    Raises:
        ValueError: If ``form_type`` is not exactly ``"10-K"`` or ``"10-Q"``
                    (WR-04 — untrusted Form field otherwise flows unvalidated
                    into ChromaDB metadata, PostgreSQL columns, and canonical_id).
    """
    if not _FORM_TYPE_RE.match(form_type):
        raise ValueError(f"Invalid form_type {form_type!r}. Expected '10-K' or '10-Q'.")
    return form_type


def _validate_period_of_report(period_of_report: str) -> str:
    """Validate *period_of_report* is an ISO YYYY-MM-DD date string.

    Raises:
        ValueError: If ``period_of_report`` does not match ``YYYY-MM-DD``
                    (WR-04).
    """
    if not _PERIOD_RE.match(period_of_report):
        raise ValueError(
            f"Invalid period_of_report {period_of_report!r}. Expected YYYY-MM-DD."
        )
    return period_of_report


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
    # Offloaded to a thread-pool executor (WR-02) — canonical_exists() makes a
    # blocking ChromaDB HTTP call.
    if await _run_sync(canonical_exists, canonical_id):
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

    # --- Prepare chunk ids/texts/metadatas for both PostgreSQL and ChromaDB ---

    ids = [f"{canonical_id}:{chunk['metadata']['chunk_index']}" for chunk in chunks]
    texts = [chunk["text"] for chunk in chunks]
    metadatas = [chunk["metadata"] for chunk in chunks]

    # --- Persist Document + DocumentChunk rows to PostgreSQL FIRST (CR-02) ---
    # Writing PostgreSQL before ChromaDB means a flush() failure (e.g. a unique
    # constraint violation) never leaves an orphaned ChromaDB write behind: no
    # ChromaDB call has happened yet, so canonical_exists() stays False and a
    # retry is clean.

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

    # --- Embed and store in ChromaDB SECOND (CR-02) ---
    # If this raises, the PostgreSQL session is rolled back by the caller's
    # except block (see ingest_ticker) before it is committed, so no Document
    # row is left referencing chunks that were never written to ChromaDB.
    # Offloaded to a thread-pool executor (WR-02) — embed_and_store() runs
    # SentenceTransformer.encode (CPU-bound) plus a blocking ChromaDB write.
    await _run_sync(embed_and_store, ids=ids, texts=texts, metadatas=metadatas)

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

    # Offloaded to a thread-pool executor per call (WR-02) — canonical_exists()
    # makes a blocking ChromaDB HTTP call; checked sequentially so the first
    # miss can short-circuit without waiting on the rest.
    all_cached = False
    if existing_canonical_ids:
        all_cached = True
        for cid in existing_canonical_ids:
            if not await _run_sync(canonical_exists, cid):
                all_cached = False
                break

    if all_cached:
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
            # Roll back any flushed-but-uncommitted Document/DocumentChunk rows
            # for this filing (CR-02) — the session is reused across filings in
            # this loop, so a failed filing must not carry pending rows into a
            # later filing's session.commit().
            await session.rollback()
            result.source_warnings.append(
                f"Failed to ingest {ticker} "
                f"{filing_meta.get('form_type')} "
                f"{filing_meta.get('period_of_report')}: {exc}"
            )

        # Rate-limit: sleep between sequential filing fetches (Pitfall 3)
        if i < len(hits) - 1:
            await asyncio.sleep(0.1)

    return result


# ---------------------------------------------------------------------------
# Public API — ingest_pdf  (INGEST-03, INGEST-05)
# ---------------------------------------------------------------------------


async def ingest_pdf(
    file_bytes: bytes,
    user_id: str,
    ticker: str,
    form_type: str,
    period_of_report: str,
    session: AsyncSession,
) -> IngestionResult:
    """Ingest a private user-uploaded PDF filing into ChromaDB + PostgreSQL.

    Extracts text via PyMuPDF, runs the same section-aware chunking pipeline as
    ``ingest_ticker``, and stores chunks tagged with ``user_id`` so ChromaDB
    ``where={"user_id": ...}`` isolation enforces INGEST-03.  Uses the identical
    ``compute_canonical_id`` formula so a PDF of an already-EDGAR-indexed filing
    is detected as cached (INGEST-05).

    Flow:
      1. Validate + uppercase the ticker (T-02-02 SSRF guard).
      2. Reject ``file_bytes`` over 50 MB before parse (T-02-03 DoS mitigation).
      3. Compute ``canonical_id`` and check ``canonical_exists`` (public scope) and
         ``canonical_exists_for_user`` (this user's private scope) BEFORE the
         expensive PyMuPDF parse (WR-06); if either is True, return with
         ``filings_cached += 1`` — **no duplicate chunk set** (INGEST-05) and no
         cross-user/public poisoning (CR-01).
      4. Extract text via ``fitz.open(stream=file_bytes, filetype="pdf")``; on any
         parse error append a ``source_warning`` and return (INGEST-04 pattern).
      5. ``section_aware_chunk`` the plain text; build chunk IDs as
         ``"{canonical_id}:{user_id}:{chunk_index}"`` for per-user namespacing.
      6. Persist ``Document(source_type=USER_UPLOAD, visibility=PRIVATE, user_id=…)``
         and ``DocumentChunk`` rows to PostgreSQL FIRST, then ``embed_and_store``
         with ``user_id=str(user_id)`` in every chunk metadata (never empty-string;
         never None — ChromaDB 0.5.x guard) — PostgreSQL-before-ChromaDB write
         order avoids a permanent orphan if the PostgreSQL flush fails (CR-02).

    Args:
        file_bytes:       Raw PDF bytes from the upload (UploadFile.read()).
        user_id:          UUID string of the uploading user (must be non-empty).
        ticker:           Ticker symbol (case-insensitive; uppercased before use).
        form_type:        SEC form type, e.g. ``"10-K"`` or ``"10-Q"``.
        period_of_report: ISO date of the reporting period, e.g. ``"2023-09-30"``.
        session:          SQLAlchemy async session for PostgreSQL writes.

    Returns:
        ``IngestionResult`` — never raises; failures surface as ``source_warnings``.

    Raises:
        ValueError: If *ticker* contains invalid characters (T-02-02), or if
                    *form_type*/*period_of_report* fail validation (WR-04).
    """
    ticker = _validate_ticker(ticker)
    form_type = _validate_form_type(form_type)
    period_of_report = _validate_period_of_report(period_of_report)
    result = IngestionResult(ticker=ticker)

    # --- DoS mitigation: 50 MB cap BEFORE parse (T-02-03) ---
    if len(file_bytes) > _MAX_PDF_BYTES:
        result.source_warnings.append(
            f"PDF exceeds 50 MB limit ({len(file_bytes):,} bytes) "
            f"for {ticker} {form_type} {period_of_report} — skipped"
        )
        return result

    # --- Dedup check — BEFORE the expensive fitz parse (WR-06) ---
    # Same canonical_id formula as the EDGAR path (INGEST-05). Checks BOTH the
    # public scope (canonical_exists) and this user's private scope
    # (canonical_exists_for_user) so neither the public EDGAR chunk set nor
    # another user's private upload can poison this check, and so a user
    # re-uploading their own PDF is still detected as cached (CR-01).
    # Offloaded to a thread-pool executor per call (WR-02) — both make blocking
    # ChromaDB HTTP calls. Checked sequentially to preserve short-circuiting:
    # canonical_exists_for_user is only called when the public check misses.
    canonical_id = compute_canonical_id(ticker, form_type, period_of_report)
    if await _run_sync(canonical_exists, canonical_id):
        result.filings_cached += 1
        return result
    if await _run_sync(canonical_exists_for_user, canonical_id, user_id):
        result.filings_cached += 1
        return result

    # --- Extract text via fitz (PyMuPDF, D-11) ---
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        pages = [page.get_text() for page in doc]
        doc.close()
        text = "\n".join(pages)
    except Exception as exc:
        result.source_warnings.append(
            f"PDF parse failed for {ticker} {form_type} {period_of_report}: {exc}"
        )
        return result

    if not text.strip():
        result.source_warnings.append(
            f"PDF produced no extractable text for {ticker} {form_type} {period_of_report}"
        )
        return result

    # --- Chunk the plain text through the shared pipeline ---
    # user_id=str(user_id) — private chunk; never "" (ChromaDB 0.5.x None guard)
    base_metadata: dict[str, Any] = {
        "canonical_id": canonical_id,
        "ticker": ticker,
        "form_type": form_type,
        "period_of_report": period_of_report,
        "user_id": str(user_id),
    }
    chunks = section_aware_chunk(text, base_metadata)
    if not chunks:
        result.source_warnings.append(
            f"No chunks produced for {ticker} {form_type} {period_of_report}"
        )
        return result

    # --- Prepare chunk ids/texts/metadatas for both PostgreSQL and ChromaDB ---
    # ID format: "{canonical_id}:{user_id}:{chunk_index}" — unique per (filing, user, chunk)
    ids = [
        f"{canonical_id}:{user_id}:{chunk['metadata']['chunk_index']}"
        for chunk in chunks
    ]
    texts = [chunk["text"] for chunk in chunks]
    metadatas = [chunk["metadata"] for chunk in chunks]

    # --- Persist Document + DocumentChunk rows to PostgreSQL FIRST (CR-02) ---
    # PostgreSQL-before-ChromaDB write order: a flush() failure here means no
    # ChromaDB write has happened yet, so canonical_exists()/
    # canonical_exists_for_user() stay False and a retry is clean.
    doc_row = Document(
        canonical_id=canonical_id,
        user_id=user_id,  # private upload — user_id set in PostgreSQL
        ticker=ticker,
        source_type=DocumentSourceType.USER_UPLOAD,
        visibility=DocumentVisibility.PRIVATE,
        title=f"{ticker} {form_type} {period_of_report}",
        url=None,  # no SEC URL for private uploads
        fetched_at=datetime.now(timezone.utc),
    )
    session.add(doc_row)
    await session.flush()  # populate doc_row.id from PostgreSQL gen_random_uuid()

    for chunk_text, meta, chunk_id in zip(texts, metadatas, ids):
        doc_chunk = DocumentChunk(
            document_id=doc_row.id,
            ticker=ticker,
            section=meta["section"],
            chunk_index=meta["chunk_index"],
            content=chunk_text,
            embedding_id=chunk_id,
        )
        session.add(doc_chunk)

    # --- Embed and store in ChromaDB with user-namespaced IDs SECOND (CR-02) ---
    # Offloaded to a thread-pool executor (WR-02) — see _run_sync docstring.
    await _run_sync(embed_and_store, ids=ids, texts=texts, metadatas=metadatas)

    await session.commit()
    result.filings_ingested += 1
    return result
