"""Unit tests for ``ticker_resolver.resolve()`` — exact + fuzzy matching, no DB.

Tests verify (03-CONTEXT.md D-01, D-02):
  - The fuzzy/local match on a company-name query ("Tell me about Apple")
    resolves to AAPL at confidence >= 0.85, and tolerates ``session=None`` by
    falling back to the module's ``_SEED_COMPANIES`` list (no DB required).
  - An exact ticker mention (a query token equal to a known ticker) resolves
    at confidence == 1.0, method == "exact".
  - ``ResolutionResult`` exposes ``ticker``, ``confidence``, ``method``, and
    ``candidates`` (a list of ``CandidateMatch``).

Plan 03-02 adds (D-01/D-02/D-03/D-04):
  - ``selected_tickers`` fast path: each entry becomes an exact-match
    ``ResolutionResult`` (confidence 1.0, method "exact") with no fuzzy/LLM
    work (powers the REQST-04 resubmit flow).
  - LLM fallback via ``call_groq`` when the fuzzy path is inconclusive,
    degrading gracefully (never raising) when ``call_groq`` fails.
  - LLM self-reported confidence is used for method "llm" (D-02).
  - Candidates are always capped at 3, ranked descending by score (D-04).

No Groq call is made anywhere in this module except through
``app.services.groq_client.call_groq`` (never the raw Groq SDK).
"""

from unittest.mock import AsyncMock, patch

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


# ---------------------------------------------------------------------------
# selected_tickers fast path (plan 03-02, D-04, REQST-04 resubmit)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_selected_tickers_fast_path() -> None:
    """selected_tickers bypasses fuzzy/LLM work — exact match, confidence 1.0."""
    with patch(
        "app.services.ticker_resolver.call_groq", new=AsyncMock()
    ) as mock_call_groq:
        results = await resolve(
            "this text is ignored", session=None, selected_tickers=["AAPL"]
        )

    mock_call_groq.assert_not_awaited()
    assert len(results) == 1
    assert results[0].ticker == "AAPL"
    assert results[0].confidence == 1.0
    assert results[0].method == "exact"


@pytest.mark.anyio
async def test_resolve_selected_tickers_multiple() -> None:
    """Each selected ticker produces its own exact-match ResolutionResult."""
    results = await resolve(
        "ignored", session=None, selected_tickers=["AAPL", "MSFT"]
    )

    assert len(results) == 2
    assert {r.ticker for r in results} == {"AAPL", "MSFT"}
    assert all(r.confidence == 1.0 and r.method == "exact" for r in results)


# ---------------------------------------------------------------------------
# LLM fallback (plan 03-02, D-01/D-02) — fuzzy-inconclusive path
# ---------------------------------------------------------------------------

#: A query with no plausible ticker/company-name span — forces the fuzzy
#: path below the usable floor so the LLM fallback is attempted.
_INCONCLUSIVE_QUERY = "zjqxvbnk qplfwm asdklqz thesis discussion"


@pytest.mark.anyio
async def test_resolve_llm_fallback_degrades_gracefully_on_not_implemented() -> None:
    """call_groq raising NotImplementedError never propagates — degrades to fuzzy candidates."""
    with patch(
        "app.services.ticker_resolver.call_groq",
        new=AsyncMock(side_effect=NotImplementedError("Groq API calls not implemented in Phase 1")),
    ) as mock_call_groq:
        results = await resolve(_INCONCLUSIVE_QUERY, session=None)

    mock_call_groq.assert_awaited()
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, ResolutionResult)
    assert len(result.candidates) <= 3


@pytest.mark.anyio
async def test_resolve_llm_fallback_uses_self_reported_confidence() -> None:
    """A parseable LLM extraction sets method 'llm' with the LLM's own confidence (D-02)."""
    with patch(
        "app.services.ticker_resolver.call_groq",
        new=AsyncMock(return_value='{"ticker": "AAPL", "confidence": 0.92}'),
    ) as mock_call_groq:
        results = await resolve(_INCONCLUSIVE_QUERY, session=None)

    mock_call_groq.assert_awaited()
    assert len(results) == 1
    result = results[0]
    assert result.method == "llm"
    assert result.ticker == "AAPL"
    assert result.confidence == 0.92


# ---------------------------------------------------------------------------
# Candidate ranking / cap (D-04)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_candidates_capped_at_three_ranked_descending() -> None:
    """candidates never exceeds 3 entries and is sorted descending by score."""
    results = await resolve("Tell me about Apple", session=None)

    result = results[0]
    assert len(result.candidates) <= 3
    scores = [c.score for c in result.candidates]
    assert scores == sorted(scores, reverse=True)
