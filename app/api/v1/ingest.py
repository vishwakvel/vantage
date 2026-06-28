"""Ingest API endpoints — SEC filing intake over HTTP.

Endpoints:
- POST /ingest/ticker  → 200 IngestionResultResponse (or 422 on invalid ticker)
- POST /ingest/pdf     → 200 IngestionResultResponse (or 401/403 if unauthenticated)

Mounted under /api/v1 by the v1 aggregator, yielding:
  /api/v1/ingest/ticker
  /api/v1/ingest/pdf

Security boundaries (STRIDE T-02-01, T-02-02, T-02-03):
- T-02-01: /ingest/pdf derives user_id ONLY from get_current_user; never from
  any request body field.  An unauthenticated upload returns 401/403.
- T-02-02: ticker is validated as 1-10 uppercase alphanumerics by the
  TickerRequest Pydantic model before any service call is made.
- T-02-03: source failures are forwarded as source_warnings in a 200 response;
  they are never converted to HTTP 5xx errors (INGEST-04).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, UploadFile
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.ingestion_service as ingestion_service
from app.core.dependencies import get_current_user, get_session
from app.db.models import User

router = APIRouter(prefix="/ingest", tags=["ingest"])

# ---------------------------------------------------------------------------
# Ticker validation — mirrors the service-level _TICKER_RE (T-02-02)
# ---------------------------------------------------------------------------

_TICKER_RE: re.Pattern[str] = re.compile(r"^[A-Z0-9]{1,10}$")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class TickerRequest(BaseModel):
    """Request body for POST /ingest/ticker.

    Attributes:
        ticker: Stock ticker symbol.  Validated to 1-10 uppercase alphanumeric
                characters (T-02-02 SSRF mitigation — prevents path-traversal
                strings like ``'../etc'`` from reaching EDGAR).
    """

    ticker: str

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, v: str) -> str:
        """Uppercase and validate the ticker symbol.

        Raises:
            ValueError: If the ticker contains characters outside [A-Z0-9] or
                        exceeds 10 characters.
        """
        normalized = v.upper()
        if not _TICKER_RE.match(normalized):
            raise ValueError(
                "ticker must be 1-10 uppercase alphanumeric characters "
                f"(received {v!r})"
            )
        return normalized


class IngestionResultResponse(BaseModel):
    """JSON response returned by both ingest endpoints.

    Mirrors ``IngestionResult`` from the service layer but expressed as a
    Pydantic model so FastAPI serialises it correctly.

    Attributes:
        ticker:           Validated, uppercased ticker symbol.
        filings_ingested: Number of filings newly embedded in this call.
        filings_cached:   Number of filings skipped (already in ChromaDB).
        source_warnings:  Non-fatal per-filing warnings (INGEST-04).
                          Always present (may be empty list); never causes 5xx.
    """

    ticker: str
    filings_ingested: int
    filings_cached: int
    source_warnings: list[str]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/ticker", response_model=IngestionResultResponse)
async def ingest_ticker_endpoint(
    body: TickerRequest,
    session: AsyncSession = Depends(get_session),
) -> IngestionResultResponse:
    """Ingest recent SEC filings for a given ticker symbol.

    Calls ``ingestion_service.ingest_ticker`` and returns the result as JSON.
    Source failures (e.g. EDGAR down) surface in ``source_warnings`` — the
    response is always HTTP 200 (INGEST-04, T-02-03).

    Args:
        body:    Validated request body containing the uppercased ticker.
        session: Injected async DB session.

    Returns:
        ``IngestionResultResponse`` with filing counts and any non-fatal warnings.
    """
    result = await ingestion_service.ingest_ticker(body.ticker, session=session)
    return IngestionResultResponse(
        ticker=result.ticker,
        filings_ingested=result.filings_ingested,
        filings_cached=result.filings_cached,
        source_warnings=result.source_warnings,
    )


@router.post("/pdf", response_model=IngestionResultResponse)
async def ingest_pdf_endpoint(
    file: UploadFile,
    ticker: str = Form(...),
    form_type: str = Form(...),
    period_of_report: str = Form(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> IngestionResultResponse:
    """Ingest a private user-uploaded PDF filing.

    Authentication is REQUIRED — the endpoint depends on ``get_current_user``
    which validates the Bearer JWT and returns the authenticated ``User``.
    An unauthenticated request never reaches the handler body (401/403).

    ``user.id`` is taken EXCLUSIVELY from the authenticated principal.  There
    is no ``user_id`` form field; providing one in the request body would be
    silently ignored (T-02-01 — user_id must not come from an untrusted source).

    Source failures (e.g. PyMuPDF parse error) surface in ``source_warnings``
    and the response remains HTTP 200 (INGEST-04, T-02-03).

    Args:
        file:             Multipart PDF upload.
        ticker:           Ticker symbol (form field; validated by service layer).
        form_type:        SEC form type, e.g. ``"10-K"`` (form field).
        period_of_report: ISO date of the reporting period (form field).
        user:             Authenticated user resolved from the Bearer JWT
                          (injected by ``get_current_user``).
        session:          Injected async DB session.

    Returns:
        ``IngestionResultResponse`` with filing counts and any non-fatal warnings.
    """
    file_bytes = await file.read()
    result = await ingestion_service.ingest_pdf(
        file_bytes=file_bytes,
        user_id=str(user.id),
        ticker=ticker,
        form_type=form_type,
        period_of_report=period_of_report,
        session=session,
    )
    return IngestionResultResponse(
        ticker=result.ticker,
        filings_ingested=result.filings_ingested,
        filings_cached=result.filings_cached,
        source_warnings=result.source_warnings,
    )
