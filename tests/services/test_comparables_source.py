"""Unit tests for ``ComparablesSource`` (05-04-PLAN.md, D-05).

Coverage:
  - get_peers: capped, de-duplicated, upper-cased peer list excluding the
    input ticker, sourced from a fake ``yfinance.Industry(...).top_companies``
    DataFrame keyed by the subject ticker's ``industryKey``.
  - get_peers: missing ``industryKey`` yields ``[]`` (no raise, no
    fabrication).
  - get_peers: empty/absent industry constituent data yields ``[]``.
  - get_peers: malformed candidate symbols are dropped (ticker-pattern
    validation, T-05-COMP-INPUT).
  - get_metrics: returns the 5 expected keys per peer.
  - get_metrics: skips a peer whose fake fetch raises, without aborting the
    batch.

Mocks only at the yfinance boundary — ``app.services.comparables_source
.yfinance.Ticker`` / ``.Industry`` — no live network calls (mirrors
tests/services/test_edgar_client.py's boundary-mock convention).
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.comparables_source import ComparablesSource, comparables_source

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_comparables_source_singleton_exists() -> None:
    """comparables_source module-level singleton is a ComparablesSource instance."""
    assert comparables_source is not None
    assert isinstance(comparables_source, ComparablesSource)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _fake_ticker_factory(info_by_symbol: dict[str, dict[str, Any] | None]) -> Any:
    """Build a fake replacing yfinance.Ticker(symbol) -> object with .info."""

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self._symbol = symbol

        @property
        def info(self) -> dict[str, Any]:
            value = info_by_symbol.get(self._symbol)
            if value is None:
                raise KeyError(f"no fake info for {self._symbol}")
            return value

    return _FakeTicker


# ---------------------------------------------------------------------------
# get_peers
# ---------------------------------------------------------------------------


async def test_get_peers_capped_deduplicated_uppercased_excludes_self() -> None:
    """get_peers returns a capped, de-duplicated, upper-cased peer list."""
    source = ComparablesSource()

    fake_info = {"AAPL": {"industryKey": "consumer-electronics"}}
    top_companies = pd.DataFrame(
        index=["aapl", "sono", "sono", "tbch", "axil", "boxl", "wto"],
        data={"name": ["x"] * 7},
    )

    with (
        patch(
            "app.services.comparables_source.yfinance.Ticker",
            new=_fake_ticker_factory(fake_info),
        ),
        patch(
            "app.services.comparables_source.yfinance.Industry",
            new=lambda key: MagicMock(top_companies=top_companies),
        ),
    ):
        peers = await source.get_peers("AAPL", limit=3)

    assert peers == ["SONO", "TBCH", "AXIL"]
    assert "AAPL" not in peers
    assert len(peers) == 3


async def test_get_peers_missing_industry_key_returns_empty() -> None:
    """get_peers returns [] when the ticker's info has no industryKey (no raise)."""
    source = ComparablesSource()

    fake_info = {"ZZZZ": {"sector": "Unknown"}}  # no industryKey

    with patch(
        "app.services.comparables_source.yfinance.Ticker",
        new=_fake_ticker_factory(fake_info),
    ):
        peers = await source.get_peers("ZZZZ")

    assert peers == []


async def test_get_peers_empty_top_companies_returns_empty() -> None:
    """get_peers returns [] when the industry lookup yields no constituents."""
    source = ComparablesSource()

    fake_info = {"AAPL": {"industryKey": "consumer-electronics"}}
    empty_df = pd.DataFrame()

    with (
        patch(
            "app.services.comparables_source.yfinance.Ticker",
            new=_fake_ticker_factory(fake_info),
        ),
        patch(
            "app.services.comparables_source.yfinance.Industry",
            new=lambda key: MagicMock(top_companies=empty_df),
        ),
    ):
        peers = await source.get_peers("AAPL")

    assert peers == []


async def test_get_peers_info_fetch_raises_returns_empty() -> None:
    """get_peers never raises: a failed Ticker(...).info fetch yields []."""
    source = ComparablesSource()

    with patch(
        "app.services.comparables_source.yfinance.Ticker",
        new=_fake_ticker_factory({}),  # no entry -> _FakeTicker.info raises KeyError
    ):
        peers = await source.get_peers("NOPE")

    assert peers == []


async def test_get_peers_drops_malformed_symbols() -> None:
    """get_peers drops candidate symbols that fail ticker-pattern validation."""
    source = ComparablesSource()

    fake_info = {"AAPL": {"industryKey": "consumer-electronics"}}
    top_companies = pd.DataFrame(
        index=["aapl", "this-is-way-too-long-to-be-a-ticker", "sono"],
        data={"name": ["x"] * 3},
    )

    with (
        patch(
            "app.services.comparables_source.yfinance.Ticker",
            new=_fake_ticker_factory(fake_info),
        ),
        patch(
            "app.services.comparables_source.yfinance.Industry",
            new=lambda key: MagicMock(top_companies=top_companies),
        ),
    ):
        peers = await source.get_peers("AAPL")

    assert peers == ["SONO"]
    assert all(len(p) <= 10 and p.isalnum() for p in peers)


# ---------------------------------------------------------------------------
# get_metrics
# ---------------------------------------------------------------------------


async def test_get_metrics_returns_expected_keys() -> None:
    """get_metrics returns the 5 expected keys per successfully-fetched peer."""
    source = ComparablesSource()

    fake_info = {
        "SONO": {
            "marketCap": 1_000_000,
            "trailingPE": 15.2,
            "profitMargins": 0.08,
            "totalRevenue": 2_000_000,
        },
        "TBCH": {
            "marketCap": 500_000,
            "trailingPE": None,
            "profitMargins": -0.02,
            "totalRevenue": 100_000,
        },
    }

    with patch(
        "app.services.comparables_source.yfinance.Ticker",
        new=_fake_ticker_factory(fake_info),
    ):
        metrics = await source.get_metrics(["SONO", "TBCH"])

    assert len(metrics) == 2
    for row in metrics:
        assert set(row.keys()) == {
            "ticker",
            "market_cap",
            "trailing_pe",
            "profit_margin",
            "revenue",
        }

    sono_row = next(r for r in metrics if r["ticker"] == "SONO")
    assert sono_row["market_cap"] == 1_000_000
    assert sono_row["trailing_pe"] == 15.2
    assert sono_row["profit_margin"] == 0.08
    assert sono_row["revenue"] == 2_000_000


async def test_get_metrics_skips_peer_whose_fake_raises() -> None:
    """get_metrics never raises: a peer whose fetch fails is skipped, batch continues."""
    source = ComparablesSource()

    fake_info = {
        "SONO": {
            "marketCap": 1_000_000,
            "trailingPE": 15.2,
            "profitMargins": 0.08,
            "totalRevenue": 2_000_000,
        },
        # "BADCO" intentionally absent -> _FakeTicker.info raises KeyError
    }

    with patch(
        "app.services.comparables_source.yfinance.Ticker",
        new=_fake_ticker_factory(fake_info),
    ):
        metrics = await source.get_metrics(["SONO", "BADCO"])

    assert len(metrics) == 1
    assert metrics[0]["ticker"] == "SONO"
