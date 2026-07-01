"""Ticker resolution service — exact + fuzzy match against companies + seed list.

``resolve(raw_query, session)`` resolves a free-text research request to zero
or more candidate tickers, deriving confidence per 03-CONTEXT.md D-02:

  - Exact match: a query token equal to a known ticker (Company.ticker rows
    when a session is supplied, unioned with ``_SEED_COMPANIES`` values) →
    confidence 1.0, method "exact".
  - Fuzzy match: no exact ticker token found. Score candidate company names
    (seed keys + Company.name rows) against sliding windows ("spans") of the
    query's tokens using ``difflib.SequenceMatcher(None, span, name).ratio()``
    — the best-scoring candidate becomes the result's ticker/confidence,
    method "fuzzy". The top 3 scored candidates are always returned as
    ``candidates``, ranked descending.

The fuzzy/local match always runs first (D-01) — this module makes **no**
Groq call; any LLM-fallback extraction is a later plan (03-02) and belongs in
this same file when added, going through ``app.services.groq_client`` only
(never a direct Groq SDK import, per the CI-enforced import guard).

The synchronous fuzzy scan is offloaded via ``_run_sync`` (a thread-pool
executor), mirroring ``ingestion_service._run_sync`` (WR-02 precedent), so
``resolve`` never blocks the event loop even over a large companies table.

Public API::

    from app.services.ticker_resolver import resolve, ResolutionResult, CandidateMatch
"""

from __future__ import annotations

import asyncio
import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company

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
                    normalized fuzzy-match ratio for a company-name match.
        method:     "exact" | "fuzzy" | "llm" (LLM fallback lands in a later
                    plan; this module only ever returns "exact" or "fuzzy").
        candidates: Ranked candidate list (top 3), descending by score.
    """

    ticker: str | None
    confidence: float
    method: str
    candidates: list[CandidateMatch] = field(default_factory=list)


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


# ---------------------------------------------------------------------------
# Public API — resolve
# ---------------------------------------------------------------------------


async def resolve(raw_query: str, session: AsyncSession | None) -> list[ResolutionResult]:
    """Resolve *raw_query* to zero or more ``ResolutionResult`` entries.

    Single-term slice: this plan always returns a list of exactly one
    ``ResolutionResult`` (multi-ticker support is a later plan). Runs the
    cheap local match first (D-01) — exact ticker-token match, then fuzzy
    company-name match — and never calls Groq.

    Args:
        raw_query: Free-text research request, e.g. "Tell me about Apple".
        session:   Async DB session used to look up ``Company`` rows. May be
                   None, in which case resolution falls back entirely to
                   ``_SEED_COMPANIES`` (used by pure unit tests with no DB).

    Returns:
        A single-element list containing the best ``ResolutionResult``.
    """
    tokens = _TOKEN_RE.findall(raw_query)
    upper_tokens = [t.upper() for t in tokens]

    # --- Exact path: a token that is both ticker-shaped AND a known ticker ---
    known_tickers: set[str] = set(_SEED_COMPANIES.values())
    if session is not None:
        rows = await session.execute(select(Company.ticker))
        known_tickers |= {row[0] for row in rows.fetchall()}

    for token in upper_tokens:
        if _TICKER_RE.match(token) and token in known_tickers:
            return [
                ResolutionResult(
                    ticker=token,
                    confidence=1.0,
                    method="exact",
                    candidates=[CandidateMatch(ticker=token, name=None, score=1.0)],
                )
            ]

    # --- Fuzzy path: score candidate company names against query spans ---
    universe: dict[str, str] = dict(_SEED_COMPANIES)
    if session is not None:
        rows = await session.execute(select(Company.ticker, Company.name))
        for ticker, name in rows.fetchall():
            if name:
                universe[name.lower()] = ticker

    spans = _generate_spans(tokens)
    scored = await _run_sync(_score_candidates, spans, universe)
    top_candidates = scored[:3]

    if not scored or scored[0].score < _MIN_USABLE_SCORE:
        return [
            ResolutionResult(
                ticker=None,
                confidence=scored[0].score if scored else 0.0,
                method="fuzzy",
                candidates=top_candidates,
            )
        ]

    best = scored[0]
    return [
        ResolutionResult(
            ticker=best.ticker,
            confidence=best.score,
            method="fuzzy",
            candidates=top_candidates,
        )
    ]
