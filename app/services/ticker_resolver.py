"""Ticker resolution service — exact + fuzzy match against companies + seed list.

``resolve(raw_query, session, selected_tickers=None)`` resolves a free-text
research request to zero or more candidate tickers, deriving confidence per
03-CONTEXT.md D-02:

  - Selected-ticker fast path (plan 03-02, REQST-04): when ``selected_tickers``
    is provided (a resubmit after a ClarificationResponse), each entry is
    validated against ``_TICKER_RE`` and treated as an exact match —
    confidence 1.0, method "exact" — with no fuzzy/LLM work at all.
  - Exact match: a query token equal to a known ticker (Company.ticker rows
    when a session is supplied, unioned with ``_SEED_COMPANIES`` values) →
    confidence 1.0, method "exact".
  - Fuzzy match: no exact ticker token found. Score candidate company names
    (seed keys + Company.name rows) against sliding windows ("spans") of the
    query's tokens using ``difflib.SequenceMatcher(None, span, name).ratio()``
    — the best-scoring candidate becomes the result's ticker/confidence,
    method "fuzzy". The top 3 scored candidates are always returned as
    ``candidates``, ranked descending (D-04).
  - LLM fallback (plan 03-02, D-01/D-02/D-03): when the fuzzy path is
    inconclusive (best score below the usable floor), ``resolve`` attempts a
    rate-limited extraction via ``app.services.groq_client.call_groq``. The
    call is wrapped in try/except — ``NotImplementedError`` (Phase 1-3 stub)
    or any other exception degrades to a low-confidence ``ResolutionResult``
    carrying the ranked fuzzy candidates, never raising. A parseable
    extraction uses the LLM's own self-reported confidence with method "llm".
  - Multi-term extraction (plan 03-03, D-06/D-07): the free-text path splits
    ``raw_query`` on comparison connectors ("and", ",", "vs", "versus",
    "compare") into up to 2 candidate spans, each resolved independently
    through the exact/fuzzy/LLM cascade above, yielding one
    ``ResolutionResult`` per span. More than 2 detected spans raises
    ``TooManyTickersError`` (T-03-09: rejected before any resolve/ingest work
    fans out).

The fuzzy/local match always runs first (D-01); the LLM is only ever reached
through ``call_groq`` (never a direct Groq SDK import, per the CI-enforced
import guard) and only when the fuzzy match is inconclusive — this keeps the
shared 6,000 tok/min Groq budget available for the agent pipeline.

The synchronous fuzzy scan is offloaded via ``_run_sync`` (a thread-pool
executor), mirroring ``ingestion_service._run_sync`` (WR-02 precedent), so
``resolve`` never blocks the event loop even over a large companies table.

Public API::

    from app.services.ticker_resolver import resolve, ResolutionResult, CandidateMatch
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company
from app.services.groq_client import call_groq

# ---------------------------------------------------------------------------
# WR-02-style offload — mirrors ingestion_service._run_sync
# ---------------------------------------------------------------------------


async def _run_sync(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous, blocking callable off the event loop.

    The fuzzy scan (``difflib.SequenceMatcher`` over every candidate name) is
    CPU-bound and can grow with the size of the ``companies`` table. Running
    it via ``loop.run_in_executor`` keeps the server responsive, matching the
    offload pattern already established for ``vector_store`` calls in
    ``ingestion_service.py`` (WR-02).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


# ---------------------------------------------------------------------------
# Ticker validation — identical contract to ingestion_service._TICKER_RE
# ---------------------------------------------------------------------------

#: Compiled pattern: 1-10 uppercase alphanumeric characters only. Kept
#: identical to ``ingestion_service._TICKER_RE`` (per 03-PATTERNS.md) so the
#: "what counts as a ticker" contract is consistent across ingestion and
#: resolution.
_TICKER_RE: re.Pattern[str] = re.compile(r"^[A-Z0-9]{1,10}$")

#: Extracts alphanumeric word tokens from a free-text query (drops
#: punctuation) for both the exact-match scan and the fuzzy span generator.
_TOKEN_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9]+")

#: Comparison connectors that separate individual ticker/company mentions in
#: a multi-ticker query (D-06/D-07), e.g. "Compare AAPL and MSFT". Matched as
#: whole words (or a bare comma) so substrings inside other words (e.g.
#: "Andover") are never treated as a split point.
_CONNECTOR_SPLIT_RE: re.Pattern[str] = re.compile(
    r"\b(?:and|vs\.?|versus|compare)\b|,", re.IGNORECASE
)

#: D-07: a research request may name at most 2 tickers.
_MAX_TERMS: int = 2


class TooManyTickersError(ValueError):
    """Raised when a request names more than ``_MAX_TERMS`` tickers (D-07).

    The router (``app/api/v1/research.py``) catches this and maps it to
    ``HTTPException(status_code=400)`` — rejected before any resolve/ingest
    work fans out (T-03-09).
    """

# ---------------------------------------------------------------------------
# Seed company list — used when the companies table has no match (or no
# session is supplied at all, e.g. in pure unit tests).
# ---------------------------------------------------------------------------

#: Lowercased common name -> ticker. A handful of well-known names is
#: sufficient for this slice; the ``companies`` table is the primary source
#: of truth once populated.
_SEED_COMPANIES: dict[str, str] = {
    "apple": "AAPL",
    "apple inc": "AAPL",
    "apple inc.": "AAPL",
    "microsoft": "MSFT",
    "microsoft corporation": "MSFT",
    "amazon": "AMZN",
    "amazon.com": "AMZN",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "tesla": "TSLA",
    "tesla inc": "TSLA",
    "meta": "META",
    "meta platforms": "META",
    "nvidia": "NVDA",
}

#: Fuzzy scores below this floor are not considered a usable match — the
#: result's ``ticker`` is set to None in that case, but ranked candidates
#: (if any) are still returned so a downstream ClarificationResponse has
#: something to show.
_MIN_USABLE_SCORE: float = 0.3

#: Sliding-window span size cap for fuzzy scoring — covers the longest seed
#: company name ("meta platforms" = 2 words) with headroom.
_MAX_SPAN_WORDS: int = 3


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CandidateMatch:
    """A single ranked ticker candidate produced during resolution.

    Attributes:
        ticker: Candidate ticker symbol.
        name:   Company name the score was computed against (None for a
                pure ticker-string match).
        score:  Match score in [0.0, 1.0] (1.0 for an exact match).
    """

    ticker: str
    name: str | None
    score: float


@dataclass
class ResolutionResult:
    """Outcome of resolving a single term within a research request.

    Attributes:
        ticker:     Resolved ticker symbol, or None if nothing scored above
                    the usable floor.
        confidence: Derived per D-02 — 1.0 for an exact ticker match, the
                    normalized fuzzy-match ratio for a company-name match, or
                    the LLM's self-reported confidence for method "llm".
        method:     "exact" | "fuzzy" | "llm".
        candidates: Ranked candidate list (top 3), descending by score.
        term:       The source text span this result was resolved from
                    (plan 03-03, D-06) — the whole query on the single-term
                    path, one split span per entry on the multi-term path, or
                    the raw selected ticker on the ``selected_tickers`` fast
                    path. Used by the router to build
                    ``ClarificationResponse.ambiguous_terms`` covering only
                    the unresolved term(s).
    """

    ticker: str | None
    confidence: float
    method: str
    candidates: list[CandidateMatch] = field(default_factory=list)
    term: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _generate_spans(tokens: list[str], max_span_words: int = _MAX_SPAN_WORDS) -> list[str]:
    """Return lowercased contiguous word windows ("spans") of *tokens*.

    Scoring the whole raw query against a short candidate name (e.g. "tell
    me about apple" vs "apple") produces a low SequenceMatcher ratio even
    when the query unambiguously names the company — spans let the fuzzy
    scan compare candidate names against the *relevant substring* of the
    query instead of the whole sentence.
    """
    spans: list[str] = []
    n = len(tokens)
    max_size = min(max_span_words, n) if n else 0
    for size in range(1, max_size + 1):
        for i in range(n - size + 1):
            spans.append(" ".join(tokens[i : i + size]).lower())
    return spans


def _split_terms(raw_query: str) -> list[str]:
    """Split *raw_query* into candidate ticker/company terms (D-06/D-07).

    Splits on comparison connectors (see ``_CONNECTOR_SPLIT_RE``) and strips
    whitespace from each resulting span, dropping empty spans. A query with
    no connector (e.g. "Tell me about Apple") is not split at all — the
    whole query becomes the single term, preserving the pre-03-03
    single-term resolution behavior exactly.
    """
    spans = _CONNECTOR_SPLIT_RE.split(raw_query)
    return [span.strip() for span in spans if span.strip()]


def _parse_llm_extraction(raw: str) -> tuple[str, float] | None:
    """Parse a ``call_groq`` response into ``(ticker, confidence)``.

    Expects a small JSON object, e.g. ``{"ticker": "AAPL", "confidence":
    0.92}``. Returns ``None`` (never raises) when the response is not
    parseable JSON, is missing either field, the ticker fails
    ``_TICKER_RE``, or the confidence is outside ``[0.0, 1.0]`` — any of
    these degrade the caller to the fuzzy-candidates fallback.
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    ticker = data.get("ticker")
    confidence = data.get("confidence")
    if not isinstance(ticker, str) or not isinstance(confidence, int | float):
        return None

    normalized_ticker = ticker.strip().upper()
    if not _TICKER_RE.match(normalized_ticker):
        return None
    if not (0.0 <= float(confidence) <= 1.0):
        return None

    return normalized_ticker, float(confidence)


def _build_extraction_prompt(raw_query: str) -> str:
    """Build a short ticker-extraction prompt for the LLM fallback (D-01/D-03).

    The prompt asks for a single JSON object so ``_parse_llm_extraction`` can
    deterministically parse the response; free-text ``raw_query`` is embedded
    as data only (never executed) — the extracted ticker is re-validated by
    ``_TICKER_RE`` regardless of what the LLM returns (T-03-08 mitigation).
    """
    return (
        "Extract the single most likely stock ticker symbol referenced in "
        "the research request below. Respond with ONLY a JSON object of the "
        'form {"ticker": "<1-10 uppercase alphanumeric ticker>", '
        '"confidence": <0.0-1.0 self-assessed confidence>}.\n\n'
        f"Research request: {raw_query!r}"
    )


def _score_candidates(spans: list[str], universe: dict[str, str]) -> list[CandidateMatch]:
    """Score every candidate name in *universe* against *spans* (sync, CPU-bound).

    *universe* maps a lowercased candidate name to its ticker. For each
    candidate, the score is the best (max) ``SequenceMatcher.ratio()`` across
    all query spans. Offloaded via ``_run_sync`` by the caller.

    Returns:
        Candidates sorted descending by score.
    """
    scored: list[CandidateMatch] = []
    for name, ticker in universe.items():
        best = 0.0
        for span in spans:
            ratio = difflib.SequenceMatcher(None, span, name).ratio()
            if ratio > best:
                best = ratio
        scored.append(CandidateMatch(ticker=ticker, name=name, score=best))
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


#: max_tokens budget acquired against groq_rate_limiter for the (short)
#: ticker extraction prompt + tiny JSON response.
_LLM_MAX_TOKENS: int = 256


async def _resolve_term(
    term: str, known_tickers: set[str], universe: dict[str, str]
) -> ResolutionResult:
    """Resolve a single term (the whole query, or one split span) to a result.

    Runs the cheap local match first (D-01) — exact ticker-token match, then
    fuzzy company-name match — and only attempts the rate-limited LLM
    fallback (via ``call_groq``) when the fuzzy path is inconclusive.
    Identical cascade to the pre-03-03 single-term ``resolve`` body; factored
    out so ``resolve`` can call it once per split term (D-06).
    """
    tokens = _TOKEN_RE.findall(term)
    upper_tokens = [t.upper() for t in tokens]

    # --- Exact path: a token that is both ticker-shaped AND a known ticker ---
    for token in upper_tokens:
        if _TICKER_RE.match(token) and token in known_tickers:
            return ResolutionResult(
                ticker=token,
                confidence=1.0,
                method="exact",
                candidates=[CandidateMatch(ticker=token, name=None, score=1.0)],
                term=term,
            )

    # --- Fuzzy path: score candidate company names against term spans ---
    spans = _generate_spans(tokens)
    scored = await _run_sync(_score_candidates, spans, universe)
    top_candidates = scored[:3]

    if scored and scored[0].score >= _MIN_USABLE_SCORE:
        best = scored[0]
        return ResolutionResult(
            ticker=best.ticker,
            confidence=best.score,
            method="fuzzy",
            candidates=top_candidates,
            term=term,
        )

    # --- LLM fallback (D-01/D-03): fuzzy path is inconclusive ---
    fallback_confidence = scored[0].score if scored else 0.0
    try:
        raw_extraction = await call_groq(
            _build_extraction_prompt(term), max_tokens=_LLM_MAX_TOKENS
        )
    except Exception:  # noqa: BLE001 — NotImplementedError (Phase 1-3 stub)
        # or any transient Groq failure degrades to the ranked fuzzy
        # candidates; the LLM fallback must never break the request (D-01).
        return ResolutionResult(
            ticker=None,
            confidence=fallback_confidence,
            method="fuzzy",
            candidates=top_candidates,
            term=term,
        )

    parsed = _parse_llm_extraction(raw_extraction)
    if parsed is None:
        return ResolutionResult(
            ticker=None,
            confidence=fallback_confidence,
            method="fuzzy",
            candidates=top_candidates,
            term=term,
        )

    llm_ticker, llm_confidence = parsed
    return ResolutionResult(
        ticker=llm_ticker,
        confidence=llm_confidence,
        method="llm",
        candidates=top_candidates,
        term=term,
    )


# ---------------------------------------------------------------------------
# Public API — resolve
# ---------------------------------------------------------------------------


async def resolve(
    raw_query: str,
    session: AsyncSession | None,
    selected_tickers: list[str] | None = None,
) -> list[ResolutionResult]:
    """Resolve *raw_query* to zero or more ``ResolutionResult`` entries.

    Splits the free-text path into up to 2 candidate terms on comparison
    connectors (D-06/D-07) and resolves each term independently through the
    exact/fuzzy/LLM cascade (D-01). A query with no connector resolves as a
    single term identical to the pre-03-03 behavior.

    Args:
        raw_query: Free-text research request, e.g. "Tell me about Apple" or
                   "Compare AAPL and MSFT". Ignored entirely when
                   ``selected_tickers`` is provided.
        session:   Async DB session used to look up ``Company`` rows. May be
                   None, in which case resolution falls back entirely to
                   ``_SEED_COMPANIES`` (used by pure unit tests with no DB).
        selected_tickers: When provided (a resubmit after a prior
                   ``ClarificationResponse`` — REQST-04), each entry is
                   treated as a pre-confirmed exact match (confidence 1.0,
                   method "exact") after validation against ``_TICKER_RE``;
                   no fuzzy/LLM work runs on this path. Entries that fail
                   ``_TICKER_RE`` are dropped rather than raised, matching
                   this module's "never raise" contract — the router already
                   validates the shape via ``ResearchRequestBody`` before
                   calling ``resolve``.

    Returns:
        One ``ResolutionResult`` per selected ticker on the
        ``selected_tickers`` fast path, otherwise one ``ResolutionResult``
        per split term (1 or 2) on the free-text path.

    Raises:
        TooManyTickersError: If the free-text path splits into more than 2
                              terms (D-07). Raised before any DB lookup or
                              fuzzy/LLM work runs (T-03-09).
    """
    # --- Fast path: pre-confirmed selection from a resubmit (D-04, REQST-04) ---
    if selected_tickers:
        results: list[ResolutionResult] = []
        for raw_ticker in selected_tickers:
            ticker = raw_ticker.strip().upper()
            if not _TICKER_RE.match(ticker):
                continue
            results.append(
                ResolutionResult(
                    ticker=ticker,
                    confidence=1.0,
                    method="exact",
                    candidates=[CandidateMatch(ticker=ticker, name=None, score=1.0)],
                    term=ticker,
                )
            )
        return results

    # --- Multi-term extraction (D-06/D-07): split before any expensive work ---
    terms = _split_terms(raw_query)
    if len(terms) > _MAX_TERMS:
        raise TooManyTickersError(
            f"A research request may name at most {_MAX_TERMS} tickers "
            f"(found {len(terms)})"
        )
    if not terms:
        terms = [raw_query]

    known_tickers: set[str] = set(_SEED_COMPANIES.values())
    if session is not None:
        rows = await session.execute(select(Company.ticker))
        known_tickers |= {row[0] for row in rows.fetchall()}

    universe: dict[str, str] = dict(_SEED_COMPANIES)
    if session is not None:
        rows = await session.execute(select(Company.ticker, Company.name))
        for ticker, name in rows.fetchall():
            if name:
                universe[name.lower()] = ticker

    return [await _resolve_term(term, known_tickers, universe) for term in terms]
