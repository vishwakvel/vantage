"""Live/spot price client — yfinance-backed (Phase 11 PRICE_MOVE evaluation).

This module answers a *live, right-now* question — what is a ticker's current
spot price — as distinct from ``financial_metrics_source.py`` (a *time-series*
question: how has one company's own fundamentals moved quarter over quarter)
and ``comparables_source.py`` (a *cross-sectional* question: who are this
ticker's peers and how do they compare right now). Phase 11's PRICE_MOVE alert
rule needs exactly this: a current price to compare against ``AlertRule.state
["last_price"]``.

yfinance imports are confined to ``app/services/`` — this module is now the
THIRD file holding them, alongside ``comparables_source.py`` and
``financial_metrics_source.py``, enforced by
``tests/test_boundaries.py::test_yfinance_imports_confined_to_services``.
Callers import the module-level ``live_price_source`` singleton below and
never import yfinance directly.

yfinance is synchronous/blocking under the hood. The price fetch is offloaded
to a worker thread via ``asyncio.to_thread`` so the alert evaluator never
blocks its event loop while iterating rules for potentially many watchlist
entries in one tick.

Deliberate deviation from 11-PATTERNS.md: that pattern map suggested a
``reset_live_price_source()`` function, mirroring the httpx-based
``edgar_client``/``news_client``/``arxiv_client``/``groq_client`` singletons'
event-loop-safety reset. This module intentionally exposes NO reset function,
because it holds no event-loop-bound state: yfinance is fully synchronous and
every call runs in a worker thread via ``asyncio.to_thread``, so unlike those
httpx ``AsyncClient``-backed singletons there is no client object bound to a
prior task's now-closed event loop that needs rebuilding. The existing
yfinance singleton ``financial_metrics_source`` likewise has no reset and is
not reset by ``app/workers/tasks.py::run_research_task``. Adding a no-op reset
here would be dead code that falsely implies loop-bound state exists — the
Celery task author in plan 11-05 should NOT call a reset for this singleton.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import yfinance


class LivePriceSource:
    """Spot price client backed by yfinance's ``fast_info``.

    One public coroutine, ``get_current_price``, fetches the current price
    for a ticker, degrading to ``None`` on any failure or unusable value
    (network error, delisted ticker, yfinance schema change, missing key,
    non-finite or non-positive value) so a single bad fetch never raises into
    the evaluator (the same graceful-degradation contract
    ``ingestion_service.py`` applies to EDGAR failures).
    """

    async def get_current_price(self, ticker: str) -> float | None:
        """Return the current spot price for *ticker*, or ``None``.

        Args:
            ticker: The subject ticker to fetch a live price for.

        Returns:
            A positive finite ``float`` price, or ``None`` if the fetch
            raised, the price was absent, or the value was unusable (NaN,
            infinite, zero, or negative — any of which would make plan
            11-03's percentage-move arithmetic undefined or divide by zero).
            Never raises.
        """
        try:
            fast_info = await asyncio.to_thread(self._fetch_fast_info, ticker)
            value = float(fast_info["last_price"])
        except Exception:
            return None

        if not math.isfinite(value) or value <= 0:
            return None

        return value

    # -------------------------------------------------------------------
    # Blocking-call isolation
    # -------------------------------------------------------------------

    @staticmethod
    def _fetch_fast_info(ticker: str) -> Any:
        """Blocking yfinance call — always invoke via asyncio.to_thread."""
        return yfinance.Ticker(ticker).fast_info


# ---------------------------------------------------------------------------
# Module-level singleton — import this; do NOT create additional instances
# ---------------------------------------------------------------------------

live_price_source: LivePriceSource = LivePriceSource()

__all__ = ["LivePriceSource", "live_price_source"]
