"""Research API endpoints — free-text request intake and resolution.

Endpoints:
- POST /research → 200 ResearchPlanResponse (unambiguous, plan created +
  ingestion triggered) or 200 ClarificationResponse (ambiguous, no plan
  created)

Mounted under /api/v1 by the v1 aggregator, yielding:
  /api/v1/research

Implements REQST-01 (free-text intake), REQST-02 (auto-resolve >= 0.85
without prompting), REQST-03 (strict clarification gate — no plan/memo until
every ticker resolves), and REQST-04 (resubmitting with ``selected_tickers``
resolves straight to a plan), per 03-CONTEXT.md D-01 through D-04.

Security boundaries (STRIDE T-03-01, T-03-04, T-03-06):
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
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
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
         Otherwise the cheap local match (exact + fuzzy) runs first, falling
         back to a rate-limited LLM extraction only when fuzzy is
         inconclusive (D-01).
      3. If every resolved term reaches ``_CONFIDENCE_THRESHOLD`` with a
         non-None ticker, set ``request.status = "RESOLVED"``, persist a
         ``ResearchPlan``, and trigger ``ingestion_service.ingest_ticker``
         per ticker (non-fatal — ingestion failures are folded into
         ``ingestion_status``, never raised).
      4. Otherwise set ``request.status = "NEEDS_CLARIFICATION"``, commit
         ONLY the ``ResearchRequest`` row, and return a
         ``ClarificationResponse`` with the top-3 candidates — no
         ``ResearchPlan``/``ResearchMemo`` is created on this path (SC#2,
         REQST-03, D-06 all-or-nothing rule).

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

    results = await ticker_resolver.resolve(
        body.raw_query, session=session, selected_tickers=body.selected_tickers
    )

    resolved_ok = bool(results) and all(
        r.confidence >= _CONFIDENCE_THRESHOLD and r.ticker is not None
        for r in results
    )

    if resolved_ok:
        resolved_tickers = [r.ticker for r in results if r.ticker is not None]
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
    # D-06 all-or-nothing rule).
    request.status = "NEEDS_CLARIFICATION"
    await session.commit()
    ambiguous_terms = [body.raw_query]
    top_candidates = results[0].candidates[:3] if results else []
    return ClarificationResponse(
        request_id=str(request.id),
        ambiguous_terms=ambiguous_terms,
        candidates=[
            CandidateMatch(ticker=c.ticker, name=c.name, score=c.score)
            for c in top_candidates
        ],
    )
