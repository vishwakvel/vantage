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
  - get_peers: a candidate outside ``_MARKET_CAP_RATIO_FLOOR``..``_CEILING``x
    the subject's market cap is dropped; an in-band candidate is kept (D-08).
  - get_peers: the sector fallback fires below ``_SECTOR_FALLBACK_MIN_PEERS``
    same-scale industry peers, emits the industry peer first, and the shared
    ``seen`` set stops a sector peer from duplicating it (D-07); the result is
    capped at ``limit``.
  - get_peers: a subject whose info has no ``marketCap`` skips market-cap
    screening entirely (zero screening fetches).
  - get_peers: an all-out-of-band candidate set still yields ``[]`` — the
    D-10 no-comparables / PARTIAL contract is intact.
  - get_peers: the OBS-02 call counter is incremented once per underlying
    yfinance fetch across the industry pass, screening, and sector fallback.

Mocks only at the yfinance boundary — ``app.services.comparables_source
.yfinance.Ticker`` / ``.Industry`` — no live network calls (mirrors
tests/services/test_edgar_client.py's boundary-mock convention).
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.services import comparables_source as _impl
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


# ---------------------------------------------------------------------------
# increment_api_call_count instrumentation (OBS-02, D-05, 12-07-PLAN.md)
# ---------------------------------------------------------------------------


async def test_get_peers_increments_once_per_fetch_actually_performed() -> None:
    """get_peers increments once per underlying fetch it actually performs
    — two on the happy path (info fetch + top-companies fetch)."""
    source = ComparablesSource()

    fake_info = {"AAPL": {"industryKey": "consumer-electronics"}}
    top_companies = pd.DataFrame(
        index=["aapl", "sono", "tbch"],
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
        patch(
            "app.services.comparables_source.increment_api_call_count",
            new_callable=AsyncMock,
        ) as mock_increment,
    ):
        await source.get_peers("AAPL")

    assert mock_increment.await_count == 2


async def test_get_peers_missing_industry_key_awards_only_the_info_fetch_increment() -> None:
    """The branch that returns early without an industryKey performs
    exactly one increment (the info fetch) and never reaches — or counts —
    the second fetch."""
    source = ComparablesSource()

    fake_info = {"ZZZZ": {"sector": "Unknown"}}  # no industryKey

    with (
        patch(
            "app.services.comparables_source.yfinance.Ticker",
            new=_fake_ticker_factory(fake_info),
        ),
        patch(
            "app.services.comparables_source.increment_api_call_count",
            new_callable=AsyncMock,
        ) as mock_increment,
    ):
        peers = await source.get_peers("ZZZZ")

    assert peers == []
    assert mock_increment.await_count == 1


async def test_get_metrics_three_peers_awards_three_increments() -> None:
    """get_metrics with three peer tickers awards exactly three
    increments."""
    source = ComparablesSource()

    fake_info = {
        "SONO": {"marketCap": 1, "trailingPE": 1, "profitMargins": 1, "totalRevenue": 1},
        "TBCH": {"marketCap": 2, "trailingPE": 2, "profitMargins": 2, "totalRevenue": 2},
        "AXIL": {"marketCap": 3, "trailingPE": 3, "profitMargins": 3, "totalRevenue": 3},
    }

    with (
        patch(
            "app.services.comparables_source.yfinance.Ticker",
            new=_fake_ticker_factory(fake_info),
        ),
        patch(
            "app.services.comparables_source.increment_api_call_count",
            new_callable=AsyncMock,
        ) as mock_increment,
    ):
        metrics = await source.get_metrics(["SONO", "TBCH", "AXIL"])

    assert len(metrics) == 3
    assert mock_increment.await_count == 3


async def test_get_peers_and_get_metrics_return_values_unchanged_when_counter_backend_fails() -> (
    None
):
    """A failing counter backend (e.g. Redis down) never changes get_peers's
    or get_metrics's return values — increment_api_call_count is fail-soft
    by construction (plan 12-03)."""
    from app.services.api_call_counter import set_current_plan_id

    source = ComparablesSource()

    fake_info = {
        "AAPL": {"industryKey": "consumer-electronics"},
        "SONO": {
            "marketCap": 1_000_000,
            "trailingPE": 15.2,
            "profitMargins": 0.08,
            "totalRevenue": 2_000_000,
        },
    }
    top_companies = pd.DataFrame(
        index=["aapl", "sono", "tbch"],
        data={"name": ["x"] * 3},
    )

    set_current_plan_id("plan-counter-fail-comparables")
    try:
        with (
            patch(
                "app.services.comparables_source.yfinance.Ticker",
                new=_fake_ticker_factory(fake_info),
            ),
            patch(
                "app.services.comparables_source.yfinance.Industry",
                new=lambda key: MagicMock(top_companies=top_companies),
            ),
            patch(
                "app.services.api_call_counter._redis",
                side_effect=ConnectionError("redis down"),
            ),
        ):
            peers = await source.get_peers("AAPL")
            metrics = await source.get_metrics(["SONO"])
    finally:
        set_current_plan_id(None)

    assert peers == ["SONO", "TBCH"]
    assert len(metrics) == 1
    assert metrics[0]["ticker"] == "SONO"


# ---------------------------------------------------------------------------
# get_peers — market-cap proximity + sector fallback (DEBT-02)
# ---------------------------------------------------------------------------
#
# Every test below patches ONLY at the yfinance boundary (Ticker / Industry /
# Sector), per the module convention. ``_FakeTicker.info`` raises for any
# unregistered symbol, so the subject AND every candidate the screening loop
# visits is registered in the fake info map with an explicit ``marketCap``.
# In-band / out-of-band magnitudes are derived from the imported ratio
# constants so the tests survive a future retune of the band.

_SUBJECT_MARKET_CAP: int = 3_000_000_000_000  # ~AAPL scale — the subject's own cap
# Ratio 1.0 sits inside [FLOOR, CEILING] by construction (FLOOR < 1 < CEILING).
_IN_BAND_MARKET_CAP: int = _SUBJECT_MARKET_CAP
# Two orders of magnitude below the floor — unambiguously screened out.
_OUT_OF_BAND_MARKET_CAP: int = int(_SUBJECT_MARKET_CAP * _impl._MARKET_CAP_RATIO_FLOOR / 100)


async def test_get_peers_drops_peers_outside_the_market_cap_band() -> None:
    """D-08 core fix: a candidate outside the market-cap band is dropped and
    an in-band candidate is kept. The subject carries no ``sectorKey`` so the
    sector fallback never enters the picture."""
    source = ComparablesSource()

    fake_info = {
        "AAPL": {
            "marketCap": _SUBJECT_MARKET_CAP,
            "industryKey": "consumer-electronics",
        },
        "MSFT": {"marketCap": _IN_BAND_MARKET_CAP},
        "TINYA": {"marketCap": _OUT_OF_BAND_MARKET_CAP},
        "TINYB": {"marketCap": _OUT_OF_BAND_MARKET_CAP},
    }
    top_companies = pd.DataFrame(
        index=["aapl", "msft", "tinya", "tinyb"],
        data={"name": ["x"] * 4},
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

    assert peers == ["MSFT"]


async def test_get_peers_falls_back_to_sector_when_industry_yields_too_few_same_scale_peers() -> (
    None
):
    """D-07: the sector fallback fires when the industry pass returns fewer
    than ``_SECTOR_FALLBACK_MIN_PEERS`` same-scale peers. The industry peer is
    emitted first, the sector peers follow, the shared ``seen`` set stops the
    sector pass from re-emitting the industry peer, and the result is capped
    at ``limit``."""
    source = ComparablesSource()

    # Fixture is built so the industry pass yields exactly ONE same-scale peer,
    # which is below _SECTOR_FALLBACK_MIN_PEERS (3) and triggers the fallback.
    fake_info = {
        "AAPL": {
            "marketCap": _SUBJECT_MARKET_CAP,
            "industryKey": "consumer-electronics",
            "sectorKey": "technology",
        },
        "INDACORP": {"marketCap": _IN_BAND_MARKET_CAP},
        "INDB": {"marketCap": _OUT_OF_BAND_MARKET_CAP},
        "INDC": {"marketCap": _OUT_OF_BAND_MARKET_CAP},
        "SECA": {"marketCap": _IN_BAND_MARKET_CAP},
        "SECB": {"marketCap": _IN_BAND_MARKET_CAP},
        "SECC": {"marketCap": _IN_BAND_MARKET_CAP},
    }
    industry_df = pd.DataFrame(
        index=["aapl", "indacorp", "indb", "indc"],
        data={"name": ["x"] * 4},
    )
    sector_df = pd.DataFrame(
        index=["seca", "secb", "secc", "indacorp"],
        data={"name": ["x"] * 4},
    )

    with (
        patch(
            "app.services.comparables_source.yfinance.Ticker",
            new=_fake_ticker_factory(fake_info),
        ),
        patch(
            "app.services.comparables_source.yfinance.Industry",
            new=lambda key: MagicMock(top_companies=industry_df),
        ),
        patch(
            "app.services.comparables_source.yfinance.Sector",
            new=lambda key: MagicMock(top_companies=sector_df),
        ),
    ):
        peers = await source.get_peers("AAPL", limit=3)

    assert peers[0] == "INDACORP"  # industry peer emitted before any sector peer
    assert peers == ["INDACORP", "SECA", "SECB"]  # sector peers follow; capped at limit=3
    assert len(peers) == len(set(peers))  # shared `seen` set dedups INDACORP across passes
    assert len(peers) <= 3


async def test_get_peers_subject_without_market_cap_skips_market_cap_screening() -> None:
    """A subject whose info has no ``marketCap`` performs ZERO screening
    fetches — ``_screen_by_market_cap`` returns ``candidates[:limit]`` before
    touching yfinance. No candidate symbol is registered in the fake info
    map, so any screening fetch would raise ``KeyError`` and silently drop
    the candidate; the industry symbols coming back intact proves the
    zero-fetch early return. This is also, by construction, why the twelve
    pre-existing tests (subject fakes with no ``marketCap``) still exercise
    today's behavior."""
    source = ComparablesSource()

    fake_info = {"AAPL": {"industryKey": "consumer-electronics"}}  # no marketCap
    top_companies = pd.DataFrame(
        index=["aapl", "peera", "peerb"],
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

    assert peers == ["PEERA", "PEERB"]


async def test_get_peers_returns_empty_when_no_candidate_is_same_scale() -> None:
    """D-10 contract: when every industry candidate is out of band, get_peers
    returns exactly ``[]`` and never raises. An empty peer list is a
    legitimate "no comparables available" signal — ``app/agents/
    comparable_companies.py`` turns it into PARTIAL with the ``no_peers``
    reason. A stricter proximity filter emptying the list is correct
    behavior, not a bug."""
    source = ComparablesSource()

    fake_info = {
        "AAPL": {
            "marketCap": _SUBJECT_MARKET_CAP,
            "industryKey": "consumer-electronics",
        },
        "TINYA": {"marketCap": _OUT_OF_BAND_MARKET_CAP},
        "TINYB": {"marketCap": _OUT_OF_BAND_MARKET_CAP},
        "TINYC": {"marketCap": _OUT_OF_BAND_MARKET_CAP},
    }
    top_companies = pd.DataFrame(
        index=["aapl", "tinya", "tinyb", "tinyc"],
        data={"name": ["x"] * 4},
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

    assert peers == []


async def test_get_peers_counts_every_screening_and_fallback_fetch() -> None:
    """OBS-02 accounting: get_peers increments the API-call counter exactly
    once per underlying yfinance fetch it performs — the subject info fetch,
    the industry list fetch, one screening fetch per industry candidate
    visited before the early break, the sector list fetch, and one screening
    fetch per sector candidate visited. Driven by the same fixture as the
    sector-fallback test so a future change to the screening early-break rule
    fails loudly instead of drifting the count silently."""
    source = ComparablesSource()

    fake_info = {
        "AAPL": {
            "marketCap": _SUBJECT_MARKET_CAP,
            "industryKey": "consumer-electronics",
            "sectorKey": "technology",
        },
        "INDACORP": {"marketCap": _IN_BAND_MARKET_CAP},
        "INDB": {"marketCap": _OUT_OF_BAND_MARKET_CAP},
        "INDC": {"marketCap": _OUT_OF_BAND_MARKET_CAP},
        "SECA": {"marketCap": _IN_BAND_MARKET_CAP},
        "SECB": {"marketCap": _IN_BAND_MARKET_CAP},
        "SECC": {"marketCap": _IN_BAND_MARKET_CAP},
    }
    industry_df = pd.DataFrame(
        index=["aapl", "indacorp", "indb", "indc"],
        data={"name": ["x"] * 4},
    )
    sector_df = pd.DataFrame(
        index=["seca", "secb", "secc", "indacorp"],
        data={"name": ["x"] * 4},
    )

    with (
        patch(
            "app.services.comparables_source.yfinance.Ticker",
            new=_fake_ticker_factory(fake_info),
        ),
        patch(
            "app.services.comparables_source.yfinance.Industry",
            new=lambda key: MagicMock(top_companies=industry_df),
        ),
        patch(
            "app.services.comparables_source.yfinance.Sector",
            new=lambda key: MagicMock(top_companies=sector_df),
        ),
        patch(
            "app.services.comparables_source.increment_api_call_count",
            new_callable=AsyncMock,
        ) as mock_increment,
    ):
        peers = await source.get_peers("AAPL", limit=3)

    expected = (
        1  # subject info fetch
        + 1  # industry top-companies fetch
        + 3  # industry screening: INDACORP (kept) + INDB + INDC, all visited before limit
        + 1  # sector top-companies fetch (industry gave < _SECTOR_FALLBACK_MIN_PEERS)
        + 2  # sector screening: SECA + SECB kept, loop breaks at the remaining budget (limit - 1)
    )
    assert peers == ["INDACORP", "SECA", "SECB"]
    assert mock_increment.await_count == expected
