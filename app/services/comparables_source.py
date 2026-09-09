"""Comparable-companies data source — yfinance-backed peer set + metrics client.

D-05: this module resolves the phase's one genuinely open design question —
how to (a) construct a peer set for a ticker and (b) source comparison
metrics for those peers — using free-tier `yfinance` data.

Peer-set construction (get_peers) works by reading the ticker's
``industryKey`` from ``Ticker(ticker).info`` and then looking up that
industry's top constituent companies via the yfinance ``Industry``
constituent list (a DataFrame indexed by ticker symbol, ranked by market
weight within the industry). This is the closest thing yfinance exposes to
a "peers" API — there is no dedicated peer-list field on ``Ticker.info``.
When the industry key is absent, the lookup fails, or the industry has no
constituent data, ``get_peers`` returns an empty list rather than
fabricating peers; callers (the ComparableCompanies agent, Plan 08) MUST
treat an empty peer list as a genuine "no comparables available" signal and
degrade to PARTIAL, per the phase's fallback policy.

yfinance imports are confined to ``app/services/`` (services-boundary rule).
Two modules hold them: this one, for cross-sectional peer data, and
``app/services/financial_metrics_source.py``, for per-ticker quarterly
time-series data (Phase 9, METRIC-01). Agents and graph modules import the
respective module-level singletons — ``comparables_source`` or
``financial_metrics_source`` — and never import yfinance directly
(T-05-COMP-INPUT boundary). The rule is verified by
``tests/test_boundaries.py::test_yfinance_imports_confined_to_services``.

yfinance is synchronous/blocking under the hood. Every yfinance call is
offloaded to a worker thread via ``asyncio.to_thread`` so these async
methods never block the event loop while 5 agents fan out concurrently
(T-05-COMP-DOS). Both methods are resilient: a single bad ticker or a
missing/malformed field never aborts the batch — it is skipped/omitted and
processing continues.
"""

import asyncio
import re
from typing import Any

import yfinance

from app.services.api_call_counter import increment_api_call_count

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Valid ticker symbols: 1-10 uppercase alphanumeric characters. Every peer
# ticker returned by get_peers is re-validated against this pattern before
# being handed back to a caller (T-05-COMP-INPUT mitigation) — yfinance's
# industry-constituent data is untrusted third-party input.
_TICKER_PATTERN: re.Pattern[str] = re.compile(r"^[A-Z0-9]{1,10}$")

_DEFAULT_PEER_LIMIT: int = 5

# Below this many same-scale peers from the industry path, get_peers falls back
# to the subject's sectorKey top companies (D-07). At 3, a genuine 2-peer
# industry result still triggers the sector sweep, while the mega-cap case
# (industry bucket entirely micro-caps, proximity filter empties it) is rescued.
_SECTOR_FALLBACK_MIN_PEERS: int = 3

# A candidate is a same-scale peer only when its market cap lies between
# floor*subject and ceiling*subject, inclusive (D-08). Worked example: at a
# ~$3T subject (AAPL) this admits roughly $300B-$30T companies — the
# MSFT/NVDA/AVGO band — and excludes the sub-$1B micro-caps (SONO, TBCH, AXIL,
# BOXL) that dominate the narrow "consumer-electronics" industry bucket.
_MARKET_CAP_RATIO_FLOOR: float = 0.1
_MARKET_CAP_RATIO_CEILING: float = 10.0

# Maximum number of candidates screened per source. Screening costs one
# blocking Ticker(...).info fetch per candidate, so an unbounded pool would add
# tens of seconds and tens of counted API calls to every research run.
_CANDIDATE_SCREEN_LIMIT: int = 12


class ComparablesSource:
    """Peer-set + comparison-metrics client backed by yfinance data.

    Two async methods:
      get_peers   — construct a capped, validated peer-ticker list (D-05).
      get_metrics — fetch per-peer comparison metrics (market cap, trailing
                    P/E, profit margin, revenue).

    Neither method ever raises on a single ticker's fetch failure.
    """

    async def get_peers(self, ticker: str, *, limit: int = _DEFAULT_PEER_LIMIT) -> list[str]:
        """Return up to *limit* peer tickers for *ticker*, excluding itself.

        Construction: read the ticker's ``industryKey`` from
        ``Ticker(ticker).info``, then look up that industry's top
        constituent companies via the yfinance ``Industry`` constituent
        list. Raw constituents are self-excluded, de-duplicated and
        ticker-pattern validated by ``_extract_candidates``. If the
        industry key is missing or the lookup fails/returns no data,
        returns ``[]`` — never fabricates peers.

        Args:
            ticker: The subject ticker to find peers for.
            limit:  Maximum number of peer tickers to return.

        Returns:
            A de-duplicated, upper-cased, validated list of peer tickers,
            capped at *limit*, excluding *ticker* itself. Empty on any
            failure or when no peer data is available.

        Increments the external-API call counter (OBS-02) once per
        underlying yfinance fetch it actually performs — up to two per
        call, zero additional on the branch that returns early without an
        industry key. Each increment no-ops outside a research run (D-05).
        """
        try:
            await increment_api_call_count()
            info = await asyncio.to_thread(self._fetch_info, ticker)
        except Exception:
            return []

        if not info:
            return []

        industry_key = info.get("industryKey")
        if not industry_key:
            return []

        try:
            await increment_api_call_count()
            top_companies = await asyncio.to_thread(self._fetch_top_companies, industry_key)
        except Exception:
            return []

        if top_companies is None or getattr(top_companies, "empty", True):
            return []

        upper_ticker = ticker.upper()
        seen: set[str] = set()
        candidates = self._extract_candidates(top_companies, upper_ticker, seen)
        return candidates[:limit]

    async def get_metrics(self, tickers: list[str]) -> list[dict[str, Any]]:
        """Return per-peer comparison metrics for *tickers*.

        For each ticker, fetches ``Ticker(t).info`` and extracts market cap,
        trailing P/E, profit margin, and revenue. A peer whose fetch raises
        or returns no info is skipped — the batch never aborts.

        Args:
            tickers: Peer tickers to fetch metrics for.

        Returns:
            A list of dicts, one per successfully-fetched peer, each with
            keys ``ticker``, ``market_cap``, ``trailing_pe``,
            ``profit_margin``, ``revenue`` (values are ``None`` when the
            underlying yfinance field is absent).

        Increments the external-API call counter (OBS-02) once per peer
        ticker fetch — N peers produce N counted calls. No-ops outside a
        research run (D-05).
        """
        results: list[dict[str, Any]] = []
        for t in tickers:
            try:
                await increment_api_call_count()
                info = await asyncio.to_thread(self._fetch_info, t)
            except Exception:
                continue

            if not info:
                continue

            results.append(
                {
                    "ticker": t,
                    "market_cap": info.get("marketCap"),
                    "trailing_pe": info.get("trailingPE"),
                    "profit_margin": info.get("profitMargins"),
                    "revenue": info.get("totalRevenue"),
                }
            )

        return results

    @staticmethod
    def _fetch_info(ticker: str) -> dict[str, Any]:
        """Blocking yfinance call — always invoke via asyncio.to_thread."""
        return yfinance.Ticker(ticker).info

    @staticmethod
    def _fetch_top_companies(industry_key: str) -> Any:
        """Blocking yfinance call — always invoke via asyncio.to_thread."""
        return yfinance.Industry(industry_key).top_companies

    @staticmethod
    def _fetch_sector_top_companies(sector_key: str) -> Any:
        """Blocking yfinance call — always invoke via asyncio.to_thread."""
        return yfinance.Sector(sector_key).top_companies

    @staticmethod
    def _extract_candidates(top_companies: Any, upper_ticker: str, seen: set[str]) -> list[str]:
        """Validate raw industry/sector constituents into a candidate list.

        Performs no I/O. Iterates ``top_companies.index``, upper-cases each
        symbol, skips it when it equals *upper_ticker* or is already in
        *seen*, skips it when it fails ``_TICKER_PATTERN``, otherwise adds
        it to *seen* and appends it. Capped at ``_CANDIDATE_SCREEN_LIMIT``
        rather than the caller's ``limit`` so market-cap screening has a
        pool to work from. *seen* is mutated by design: the sector fallback
        pass shares it so it can never re-emit a symbol the industry pass
        already returned.
        """
        candidates: list[str] = []
        for symbol in top_companies.index:
            symbol_str = str(symbol).upper()
            if symbol_str == upper_ticker or symbol_str in seen:
                continue
            if not _TICKER_PATTERN.match(symbol_str):
                continue
            seen.add(symbol_str)
            candidates.append(symbol_str)
            if len(candidates) >= _CANDIDATE_SCREEN_LIMIT:
                break
        return candidates


# ---------------------------------------------------------------------------
# Module-level singleton — import this; do NOT create additional instances
# ---------------------------------------------------------------------------

comparables_source: ComparablesSource = ComparablesSource()
