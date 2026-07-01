"""Research API endpoints — free-text request intake and resolution.

Endpoints:
- POST /research → 200 ResearchPlanResponse (unambiguous, plan created +
  ingestion triggered) or 200 ClarificationResponse (ambiguous, no plan
  created)
- POST /research/{plan_id}/documents → 200 ResearchDocumentResponse (private
  PDF attached to an owned plan) or 404 (plan not found/not owned) or 413
  (oversized upload)

Mounted under /api/v1 by the v1 aggregator, yielding:
  /api/v1/research
  /api/v1/research/{plan_id}/documents

Implements REQST-01 (free-text intake), REQST-02 (auto-resolve >= 0.85
without prompting), REQST-03 (strict clarification gate — no plan/memo until
every ticker resolves), REQST-04 (resubmitting with ``selected_tickers``
resolves straight to a plan), REQST-05 (multi-ticker requests — up to 2
tickers per query, all-or-nothing gating, >2 rejected with 400), and
REQST-06 (private PDF attachment to an owned research plan), per
03-CONTEXT.md D-01 through D-07.

Security boundaries (STRIDE T-03-01, T-03-02, T-03-03, T-03-04, T-03-05,
T-03-06):
- T-03-04: /research derives the user ONLY from get_current_user; never from
  any request body field. An unauthenticated request returns 401/403 before
  any resolve/ingest work happens.
- T-03-06: raw_query is length-capped by ResearchRequestBody's field_validator
  before resolve() runs, guarding against oversized-query DoS.
- T-03-01: only tickers that resolve via ticker_resolver (which itself
  enforces the 1-10 uppercase alphanumeric _TICKER_RE contract) ever reach
  ingestion_service.ingest_ticker, which re-validates independently
  (defense in depth, mirrors T-02-02). ``selected_tickers`` is validated
  twice — once in ``ResearchRequestBody.validate_selected_tickers`` and again
  inside ``ticker_resolver.resolve``'s fast path — before it can influence
  ingestion.
- T-03-02 (IDOR/A01): POST /research/{plan_id}/documents looks up the plan
  with ``ResearchPlan.user_id == user.id`` — a non-owned or non-existent
  plan_id returns 404 in both cases (no 403-vs-404 existence leak).
- T-03-03 (DoS): the document upload is size-guarded both pre-read
  (Content-Length header) and post-read (actual byte count) against
  ``_RESEARCH_DOC_MAX_BYTES``, mirroring ``ingest_pdf_endpoint`` (CR-03).
- T-03-05 (elevation of privilege): user_id passed to ``ingestion_service.
  ingest_pdf`` is sourced EXCLUSIVELY from the authenticated principal —
  there is no user_id form field on the document-attach endpoint.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.ingestion_service as ingestion_service
import app.services.ticker_resolver as ticker_resolver
from app.core.dependencies import get_current_user, get_session
from app.db.models import ResearchPlan, ResearchPlanStatus, ResearchRequest, User

router = APIRouter(prefix="/research", tags=["research"])

#: REQST-02: uniform auto-resolve threshold applied to whichever path
#: (exact/fuzzy/llm) produced the confidence value (D-02).
_CONFIDENCE_THRESHOLD: float = 0.85

#: T-03-06: caps the untrusted raw_query body field before any resolve work.
_MAX_QUERY_LENGTH: int = 2000

#: D-07: multi-ticker requests are capped at 2 tickers.
_MAX_SELECTED_TICKERS: int = 2

#: Identical contract to ticker_resolver._TICKER_RE — 1-10 uppercase
#: alphanumeric characters (T-03-01 defense in depth).
_TICKER_RE: re.Pattern[str] = re.compile(r"^[A-Z0-9]{1,10}$")

#: Mirrors ingest.py's _PDF_ENDPOINT_MAX_BYTES — enforced before the full
#: body is buffered in memory (T-03-03 DoS mitigation, CR-03).
_RESEARCH_DOC_MAX_BYTES: int = 50 * 1024 * 1024  # 50 MB


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ResearchRequestBody(BaseModel):
    """Request body for POST /research.

    Attributes:
        raw_query: Free-text research request, e.g. "Tell me about Apple".
                   Validated non-empty and length-capped at 2000 characters
                   (T-03-06 DoS mitigation).
        selected_tickers: Optional resubmit payload (REQST-04) — the ticker(s)
                   the user picked from a prior ``ClarificationResponse``'s
                   candidates. Each entry is uppercased and validated against
                   the 1-10 uppercase-alphanumeric ticker contract; the whole
                   list is capped at 2 entries (D-07).
    """

    raw_query: str
    selected_tickers: list[str] | None = None

    @field_validator("raw_query")
    @classmethod
    def validate_raw_query(cls, v: str) -> str:
        """Reject empty/whitespace-only queries and oversized payloads.

        Raises:
            ValueError: If ``raw_query`` is empty/whitespace, or exceeds
                        ``_MAX_QUERY_LENGTH`` characters.
        """
        if not v.strip():
            raise ValueError("raw_query must not be empty")
        if len(v) > _MAX_QUERY_LENGTH:
            raise ValueError(
                f"raw_query exceeds {_MAX_QUERY_LENGTH} character limit"
            )
        return v

    @field_validator("selected_tickers")
    @classmethod
    def validate_selected_tickers(cls, v: list[str] | None) -> list[str] | None:
        """Uppercase + validate each selected ticker; reject more than 2 (D-07).

        Raises:
            ValueError: If the list has more than ``_MAX_SELECTED_TICKERS``
                        entries, or any entry fails the 1-10 uppercase
                        alphanumeric ticker contract (T-03-01 defense in
                        depth — re-validated again in
                        ``ticker_resolver.resolve``'s fast path).
        """
        if v is None:
            return v
        if len(v) > _MAX_SELECTED_TICKERS:
            raise ValueError(
                f"selected_tickers accepts at most {_MAX_SELECTED_TICKERS} entries"
            )
        normalized: list[str] = []
        for ticker in v:
            candidate = ticker.strip().upper()
            if not _TICKER_RE.match(candidate):
                raise ValueError(f"invalid ticker in selected_tickers: {ticker!r}")
            normalized.append(candidate)
        return normalized


class CandidateMatch(BaseModel):
    """A single ranked ticker candidate surfaced in a ClarificationResponse."""

    ticker: str
    name: str | None
    score: float


class ClarificationResponse(BaseModel):
    """Returned when the request cannot be auto-resolved (confidence < 0.85).

    No ``ResearchPlan``/``ResearchMemo`` is created until every ticker in the
    request resolves (PROJECT.md Key Decisions; 03-CONTEXT.md phase boundary).
    """

    needs_clarification: bool = True
    request_id: str
    ambiguous_terms: list[str]
    candidates: list[CandidateMatch]


class ResearchPlanResponse(BaseModel):
    """Returned when the request resolves unambiguously (SC#1)."""

    needs_clarification: bool = False
    plan_id: str
    request_id: str
    resolved_tickers: list[str]
    status: str
    ingestion_status: str | None


class ResearchDocumentResponse(BaseModel):
    """Returned by POST /research/{plan_id}/documents (SC#5, REQST-06)."""

    plan_id: str
    ticker: str
    filings_ingested: int
    filings_cached: int
    source_warnings: list[str]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=None)
async def create_research_request(
    body: ResearchRequestBody,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ResearchPlanResponse | ClarificationResponse:
    """Accept a free-text research request and resolve it to a ticker.

    Authentication is REQUIRED (T-03-04) — an unauthenticated request never
    reaches the handler body (401/403). Without this guard, any
    unauthenticated client could trigger the fuzzy-match scan and downstream
    EDGAR ingestion for an arbitrary query.

    Flow:
      1. Persist a ``ResearchRequest`` row with the raw query.
      2. Run ``ticker_resolver.resolve`` — when ``body.selected_tickers`` is
         provided (a resubmit after a prior ``ClarificationResponse``,
         REQST-04), it takes an exact-match fast path with no fuzzy/LLM work.
         Otherwise the free-text path splits the query into up to 2 terms on
         comparison connectors (D-06/D-07) and each term runs the cheap local
         match (exact + fuzzy) first, falling back to a rate-limited LLM
         extraction only when fuzzy is inconclusive (D-01). A query naming
         more than 2 tickers raises ``TooManyTickersError``, mapped here to
         ``HTTPException(400)`` (D-07) before any resolve/ingest work fans
         out.
      3. If every resolved term reaches ``_CONFIDENCE_THRESHOLD`` with a
         non-None ticker, set ``request.status = "RESOLVED"``, persist a
         single ``ResearchPlan`` with the de-duplicated list of resolved
         tickers, and trigger ``ingestion_service.ingest_ticker`` per ticker
         (non-fatal — ingestion failures are folded into
         ``ingestion_status``, never raised).
      4. Otherwise (all-or-nothing, D-06) set ``request.status =
         "NEEDS_CLARIFICATION"``, commit ONLY the ``ResearchRequest`` row,
         and return a ``ClarificationResponse`` whose ``ambiguous_terms`` and
         ``candidates`` cover only the unresolved term(s) — no
         ``ResearchPlan``/``ResearchMemo`` is created on this path (SC#2,
         REQST-03, D-06 all-or-nothing rule), even when other terms in the
         same request resolved unambiguously.

    Args:
        body:    Validated request body containing the raw free-text query
                 and an optional ``selected_tickers`` resubmit payload.
        user:    Authenticated user resolved from the Bearer JWT (T-03-04);
                 the ONLY source of user identity — never a body field.
        session: Injected async DB session.

    Returns:
        ``ResearchPlanResponse`` on unambiguous resolution, otherwise
        ``ClarificationResponse``.
    """
    request = ResearchRequest(
        user_id=user.id, raw_query=body.raw_query, status="PENDING"
    )
    session.add(request)
    await session.flush()  # populate request.id for the ResearchPlan FK below

    try:
        results = await ticker_resolver.resolve(
            body.raw_query, session=session, selected_tickers=body.selected_tickers
        )
    except ticker_resolver.TooManyTickersError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # D-06 all-or-nothing gate: every term (1 or 2) must resolve at or above
    # the auto-resolve threshold, or the WHOLE request is treated as
    # ambiguous — not just the unresolved term(s).
    unresolved = [
        r
        for r in results
        if r.ticker is None or r.confidence < _CONFIDENCE_THRESHOLD
    ]

    if results and not unresolved:
        # De-duplicated in case a query names the same ticker twice.
        resolved_tickers = list(
            dict.fromkeys(r.ticker for r in results if r.ticker is not None)
        )
        request.status = "RESOLVED"
        plan = ResearchPlan(
            request_id=request.id,
            user_id=user.id,
            resolved_tickers=resolved_tickers,
            status=ResearchPlanStatus.INGESTION,
        )
        session.add(plan)
        await session.commit()

        # Non-fatal ingestion trigger — never raise on failure (mirrors
        # INGEST-04's source_warnings-not-5xx convention).
        ingestion_warnings: list[str] = []
        for ticker in resolved_tickers:
            try:
                ingest_result = await ingestion_service.ingest_ticker(
                    ticker, session=session
                )
                ingestion_warnings.extend(ingest_result.source_warnings)
            except Exception as exc:  # noqa: BLE001 — non-fatal by design
                ingestion_warnings.append(f"ingestion failed for {ticker}: {exc}")

        ingestion_status = "; ".join(ingestion_warnings) if ingestion_warnings else None

        return ResearchPlanResponse(
            plan_id=str(plan.id),
            request_id=str(request.id),
            resolved_tickers=resolved_tickers,
            status=str(plan.status.value),
            ingestion_status=ingestion_status,
        )

    # Ambiguous — persist ONLY the ResearchRequest row (status flipped to
    # NEEDS_CLARIFICATION); no ResearchPlan is instantiated at all on this
    # branch, so no ResearchMemo can ever exist for it either (SC#2, REQST-03,
    # D-06 all-or-nothing rule). ambiguous_terms/candidates cover ONLY the
    # unresolved term(s) — a term that already resolved unambiguously is not
    # re-surfaced, even though the request as a whole still blocks.
    request.status = "NEEDS_CLARIFICATION"
    await session.commit()
    ambiguous_source = unresolved or results
    ambiguous_terms = [r.term for r in ambiguous_source if r.term] or [
        body.raw_query
    ]
    top_candidates: list[ticker_resolver.CandidateMatch] = []
    for r in ambiguous_source:
        top_candidates.extend(r.candidates)
    top_candidates = top_candidates[:3]
    return ClarificationResponse(
        request_id=str(request.id),
        ambiguous_terms=ambiguous_terms,
        candidates=[
            CandidateMatch(ticker=c.ticker, name=c.name, score=c.score)
            for c in top_candidates
        ],
    )


@router.post("/{plan_id}/documents", response_model=ResearchDocumentResponse)
async def attach_document(
    plan_id: str,
    request: Request,
    file: UploadFile,
    form_type: str = Form(...),
    period_of_report: str = Form(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ResearchDocumentResponse:
    """Attach a private PDF to an existing, user-owned ResearchPlan (SC#5).

    Authentication is REQUIRED (T-03-04) — an unauthenticated request never
    reaches the handler body (401/403).

    Ownership is checked against ``ResearchPlan.user_id == user.id``
    (T-03-02, OWASP A01) — a ``plan_id`` that does not exist OR does not
    belong to the authenticated user returns 404 in both cases, never 403,
    so the response never leaks whether the plan exists at all.

    Uploads are size-checked BEFORE the body is fully buffered in memory
    (T-03-03, CR-03): a Content-Length over ``_RESEARCH_DOC_MAX_BYTES`` is
    rejected immediately, and the actual byte count is re-checked after
    ``file.read()`` in case the client omitted or understated Content-Length.

    Reuses ``ingestion_service.ingest_pdf`` — the same Phase 2
    isolation-safe ingestion path used by ``POST /ingest/pdf`` — with
    ``user_id`` sourced EXCLUSIVELY from the authenticated principal
    (T-03-05); there is no ``user_id`` form field. ``ticker`` is derived from
    the plan's own ``resolved_tickers`` (D-05), never from client input.

    Source failures (e.g. PyMuPDF parse error) surface in
    ``source_warnings``; the response remains HTTP 200.

    Args:
        plan_id:           ResearchPlan UUID from the URL path.
        request:            Raw ASGI request — used to inspect Content-Length
                            before reading the body (CR-03).
        file:               Multipart PDF upload.
        form_type:          SEC form type, e.g. ``"10-K"`` (form field).
        period_of_report:   ISO date of the reporting period (form field).
        user:               Authenticated user resolved from the Bearer JWT.
        session:            Injected async DB session.

    Returns:
        ``ResearchDocumentResponse`` with the plan_id and ingestion counts.

    Raises:
        HTTPException: 404 if the plan does not exist or is not owned by
                        ``user``; 413 if the upload exceeds
                        ``_RESEARCH_DOC_MAX_BYTES``.
    """
    result = await session.execute(
        select(ResearchPlan).where(
            ResearchPlan.id == plan_id, ResearchPlan.user_id == user.id
        )
    )
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="Research plan not found")

    # Reject oversized uploads before reading the body into memory
    # (T-03-03, CR-03). Cooperative-client check only — the post-read check
    # below guards against an absent or understated Content-Length.
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > _RESEARCH_DOC_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Upload exceeds 50 MB limit")

    file_bytes = await file.read()

    # Post-read guard: catches clients that omit or understate Content-Length.
    if len(file_bytes) > _RESEARCH_DOC_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Upload exceeds 50 MB limit")

    resolved_tickers = plan.resolved_tickers or []
    ticker = resolved_tickers[0] if resolved_tickers else ""

    ingest_result = await ingestion_service.ingest_pdf(
        file_bytes=file_bytes,
        user_id=str(user.id),
        ticker=ticker,
        form_type=form_type,
        period_of_report=period_of_report,
        session=session,
    )

    return ResearchDocumentResponse(
        plan_id=str(plan.id),
        ticker=ingest_result.ticker,
        filings_ingested=ingest_result.filings_ingested,
        filings_cached=ingest_result.filings_cached,
        source_warnings=ingest_result.source_warnings,
    )
