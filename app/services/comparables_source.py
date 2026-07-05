"""Comparable-companies data source — yfinance-backed peer set + metrics client.

D-05: this module resolves the phase's one genuinely open design question —
how to (a) construct a peer set for a ticker and (b) source comparison
metrics for those peers — using free-tier `yfinance` data.

Peer-set construction (get_peers) works by reading the ticker's
``industryKey`` from ``Ticker(ticker).info`` and then looking up that
industry's top constituent companies via ``yfinance.Industry(industry_key)
.top_companies`` (a DataFrame indexed by ticker symbol, ranked by market
weight within the industry). This is the closest thing yfinance exposes to
a "peers" API — there is no dedicated peer-list field on ``Ticker.info``.
When the industry key is absent, the lookup fails, or the industry has no
constituent data, ``get_peers`` returns an empty list rather than
fabricating peers; callers (the ComparableCompanies agent, Plan 08) MUST
treat an empty peer list as a genuine "no comparables available" signal and
degrade to PARTIAL, per the phase's fallback policy.

This is the ONLY module in the codebase that imports yfinance (services-
boundary rule) — agents must import the module-level ``comparables_source``
singleton and never import yfinance directly (T-05-COMP-INPUT boundary,
enforced by grep in this plan's acceptance criteria / tests/test_boundaries.py
convention).

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Valid ticker symbols: 1-10 uppercase alphanumeric characters. Every peer
# ticker returned by get_peers is re-validated against this pattern before
# being handed back to a caller (T-05-COMP-INPUT mitigation) — yfinance's
# industry-constituent data is untrusted third-party input.
_TICKER_PATTERN: re.Pattern[str] = re.compile(r"^[A-Z0-9]{1,10}$")

_DEFAULT_PEER_LIMIT: int = 5


class ComparablesSource:
    """Peer-set + comparison-metrics client backed by yfinance.

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
        constituent companies via ``yfinance.Industry(industry_key)
        .top_companies``. If the industry key is missing or the lookup
        fails/returns no data, returns ``[]`` — never fabricates peers.

        Args:
            ticker: The subject ticker to find peers for.
            limit:  Maximum number of peer tickers to return.

        Returns:
            A de-duplicated, upper-cased, validated list of peer tickers,
            capped at *limit*, excluding *ticker* itself. Empty on any
            failure or when no peer data is available.
        """
        try:
            info = await asyncio.to_thread(self._fetch_info, ticker)
        except Exception:
            return []

        if not info:
            return []

        industry_key = info.get("industryKey")
        if not industry_key:
            return []

        try:
            top_companies = await asyncio.to_thread(self._fetch_top_companies, industry_key)
        except Exception:
            return []

        if top_companies is None or getattr(top_companies, "empty", True):
            return []

        upper_ticker = ticker.upper()
        seen: set[str] = set()
        peers: list[str] = []
        for symbol in top_companies.index:
            symbol_str = str(symbol).upper()
            if symbol_str == upper_ticker or symbol_str in seen:
                continue
            if not _TICKER_PATTERN.match(symbol_str):
                continue
            seen.add(symbol_str)
            peers.append(symbol_str)
            if len(peers) >= limit:
                break

        return peers

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
        """
        results: list[dict[str, Any]] = []
        for t in tickers:
            try:
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


# ---------------------------------------------------------------------------
# Module-level singleton — import this; do NOT create additional instances
# ---------------------------------------------------------------------------

comparables_source: ComparablesSource = ComparablesSource()
