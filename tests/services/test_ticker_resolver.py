"""Unit tests for ``ticker_resolver.resolve()`` — exact + fuzzy matching, no DB.

Tests verify (03-CONTEXT.md D-01, D-02):
  - The fuzzy/local match on a company-name query ("Tell me about Apple")
    resolves to AAPL at confidence >= 0.85, and tolerates ``session=None`` by
    falling back to the module's ``_SEED_COMPANIES`` list (no DB required).
  - An exact ticker mention (a query token equal to a known ticker) resolves
    at confidence == 1.0, method == "exact".
  - ``ResolutionResult`` exposes ``ticker``, ``confidence``, ``method``, and
    ``candidates`` (a list of ``CandidateMatch``).

No Groq call is made anywhere in this module (LLM fallback lands in a later
plan) — these are pure unit tests with no network/DB dependency.
"""

import pytest

from app.services.ticker_resolver import CandidateMatch, ResolutionResult, resolve

# ---------------------------------------------------------------------------
# Fuzzy company-name match (D-01, D-02)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_apple_company_name_fuzzy_match() -> None:
    """resolve('Tell me about Apple') resolves to AAPL at confidence >= 0.85."""
    results = await resolve("Tell me about Apple", session=None)

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, ResolutionResult)
    assert result.ticker == "AAPL"
    assert result.method in ("fuzzy", "exact")
    assert result.confidence >= 0.85


@pytest.mark.anyio
async def test_resolve_tolerates_none_session_seed_fallback() -> None:
    """resolve() falls back to _SEED_COMPANIES when session is None (no DB)."""
    results = await resolve("Microsoft", session=None)

    assert results[0].ticker == "MSFT"
    assert results[0].confidence >= 0.85
    assert results[0].method in ("fuzzy", "exact")


# ---------------------------------------------------------------------------
# Exact ticker mention (D-02)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_exact_ticker_mention() -> None:
    """A query containing the literal ticker 'AAPL' resolves at confidence 1.0."""
    results = await resolve("Tell me about AAPL", session=None)

    assert len(results) == 1
    result = results[0]
    assert result.ticker == "AAPL"
    assert result.confidence == 1.0
    assert result.method == "exact"


@pytest.mark.anyio
async def test_resolve_exact_ticker_only_query() -> None:
    """A bare ticker-only query ('AAPL') also resolves exactly at confidence 1.0."""
    results = await resolve("AAPL", session=None)

    result = results[0]
    assert result.ticker == "AAPL"
    assert result.confidence == 1.0
    assert result.method == "exact"


# ---------------------------------------------------------------------------
# ResolutionResult / CandidateMatch shape
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolution_result_shape() -> None:
    """ResolutionResult exposes ticker, confidence, method, candidates."""
    results = await resolve("Tell me about Apple", session=None)

    result = results[0]
    assert hasattr(result, "ticker")
    assert hasattr(result, "confidence")
    assert hasattr(result, "method")
    assert isinstance(result.candidates, list)
    if result.candidates:
        assert isinstance(result.candidates[0], CandidateMatch)
        assert hasattr(result.candidates[0], "ticker")
        assert hasattr(result.candidates[0], "name")
        assert hasattr(result.candidates[0], "score")
