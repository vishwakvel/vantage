"""Unit tests for ``FinancialMetricsSource`` (09-03-PLAN.md, D-01/D-02/D-03).

Coverage:
  - the ``financial_metrics_source`` singleton exists and is a
    ``FinancialMetricsSource`` instance.
  - a fully-populated set of quarterly frames yields all six core metric
    names.
  - gross_margin, operating_margin, and debt_to_equity equal their computed
    ratios for the matching quarter-end period.
  - a zero-revenue quarter suppresses only the two revenue-derived margins
    (gross_margin, operating_margin) while revenue itself is still emitted
    for that quarter.
  - NaN and infinite figures produce no row for that metric/period pair.
  - a raising balance-sheet fetch still yields income-statement and
    cash-flow metrics (per-statement resilience, T-09-PARTIAL).
  - all three statement fetches raising yields ``[]`` with no exception.
  - ``period`` strings match the ``YYYY-MM-DD`` shape.
  - an alternate row label from the alias tuples (``"Operating Revenue"`` in
    place of ``"Total Revenue"``) still resolves.

Mocks only at the yfinance boundary — ``app.services.financial_metrics_source
.yfinance.Ticker`` — no live network calls (mirrors
tests/services/test_comparables_source.py's boundary-mock convention).
"""

import math
import re
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest

from app.services.financial_metrics_source import (
    CORE_METRICS,
    METRIC_DEBT_TO_EQUITY,
    METRIC_FREE_CASH_FLOW,
    METRIC_GROSS_MARGIN,
    METRIC_NET_INCOME,
    METRIC_OPERATING_MARGIN,
    METRIC_REVENUE,
    FinancialMetricsSource,
    financial_metrics_source,
)

pytestmark = pytest.mark.anyio

_PERIOD_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_QUARTER_COLUMNS = [
    pd.Timestamp("2024-03-31"),
    pd.Timestamp("2023-12-31"),
    pd.Timestamp("2023-09-30"),
    pd.Timestamp("2023-06-30"),
]

_EXPECTED_PERIODS = {"2024-03-31", "2023-12-31", "2023-09-30", "2023-06-30"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _fake_ticker_factory(
    *,
    income_stmt: pd.DataFrame | None = None,
    balance_sheet: pd.DataFrame | None = None,
    cashflow: pd.DataFrame | None = None,
    raise_on: set[str] | None = None,
) -> Any:
    """Build a fake replacing yfinance.Ticker(symbol) with statement properties."""
    raising = raise_on or set()

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self._symbol = symbol

        @property
        def quarterly_income_stmt(self) -> pd.DataFrame:
            if "income_stmt" in raising:
                raise RuntimeError("boom: income_stmt")
            return income_stmt

        @property
        def quarterly_balance_sheet(self) -> pd.DataFrame:
            if "balance_sheet" in raising:
                raise RuntimeError("boom: balance_sheet")
            return balance_sheet

        @property
        def quarterly_cashflow(self) -> pd.DataFrame:
            if "cashflow" in raising:
                raise RuntimeError("boom: cashflow")
            return cashflow

    return _FakeTicker


def _full_income_stmt(
    *,
    revenue_label: str = "Total Revenue",
    revenue: list[float] | None = None,
    gross_profit: list[float] | None = None,
    operating_income: list[float] | None = None,
    net_income: list[float] | None = None,
) -> pd.DataFrame:
    revenue = revenue if revenue is not None else [1000.0, 900.0, 800.0, 700.0]
    gross_profit = gross_profit if gross_profit is not None else [400.0, 360.0, 320.0, 280.0]
    operating_income = (
        operating_income if operating_income is not None else [200.0, 180.0, 160.0, 140.0]
    )
    net_income = net_income if net_income is not None else [100.0, 90.0, 80.0, 70.0]

    return pd.DataFrame(
        {
            col: [revenue[i], gross_profit[i], operating_income[i], net_income[i]]
            for i, col in enumerate(_QUARTER_COLUMNS)
        },
        index=[revenue_label, "Gross Profit", "Operating Income", "Net Income"],
    )


def _full_balance_sheet(
    *,
    total_debt: list[float] | None = None,
    equity: list[float] | None = None,
) -> pd.DataFrame:
    total_debt = total_debt if total_debt is not None else [500.0, 500.0, 500.0, 500.0]
    equity = equity if equity is not None else [1000.0, 1000.0, 1000.0, 1000.0]

    return pd.DataFrame(
        {col: [total_debt[i], equity[i]] for i, col in enumerate(_QUARTER_COLUMNS)},
        index=["Total Debt", "Stockholders Equity"],
    )


def _full_cashflow(*, fcf: list[float] | None = None) -> pd.DataFrame:
    fcf = fcf if fcf is not None else [150.0, 140.0, 130.0, 120.0]

    return pd.DataFrame(
        {col: [fcf[i]] for i, col in enumerate(_QUARTER_COLUMNS)},
        index=["Free Cash Flow"],
    )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_financial_metrics_source_singleton_exists() -> None:
    """financial_metrics_source module-level singleton is a FinancialMetricsSource."""
    assert financial_metrics_source is not None
    assert isinstance(financial_metrics_source, FinancialMetricsSource)


# ---------------------------------------------------------------------------
# Fully-populated frames
# ---------------------------------------------------------------------------


async def test_fully_populated_frames_yield_all_six_metric_names() -> None:
    """A fully-populated set of frames produces rows for all six core metrics."""
    source = FinancialMetricsSource()

    with patch(
        "app.services.financial_metrics_source.yfinance.Ticker",
        new=_fake_ticker_factory(
            income_stmt=_full_income_stmt(),
            balance_sheet=_full_balance_sheet(),
            cashflow=_full_cashflow(),
        ),
    ):
        rows = await source.get_quarterly_metrics("AAPL")

    produced_metric_names = {row["metric_name"] for row in rows}
    assert produced_metric_names == set(CORE_METRICS)


async def test_gross_margin_operating_margin_debt_to_equity_match_computed_ratios() -> None:
    """Derived ratios equal the expected computation for the matching period."""
    source = FinancialMetricsSource()

    with patch(
        "app.services.financial_metrics_source.yfinance.Ticker",
        new=_fake_ticker_factory(
            income_stmt=_full_income_stmt(),
            balance_sheet=_full_balance_sheet(),
            cashflow=_full_cashflow(),
        ),
    ):
        rows = await source.get_quarterly_metrics("AAPL")

    by_metric_period = {(row["metric_name"], row["period"]): row["value"] for row in rows}

    assert by_metric_period[(METRIC_GROSS_MARGIN, "2024-03-31")] == pytest.approx(0.4)
    assert by_metric_period[(METRIC_OPERATING_MARGIN, "2024-03-31")] == pytest.approx(0.2)
    assert by_metric_period[(METRIC_DEBT_TO_EQUITY, "2024-03-31")] == pytest.approx(0.5)

    assert by_metric_period[(METRIC_GROSS_MARGIN, "2023-06-30")] == pytest.approx(280.0 / 700.0)
    assert by_metric_period[(METRIC_OPERATING_MARGIN, "2023-06-30")] == pytest.approx(
        140.0 / 700.0
    )
    assert by_metric_period[(METRIC_DEBT_TO_EQUITY, "2023-06-30")] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Zero-revenue division guard
# ---------------------------------------------------------------------------


async def test_zero_revenue_quarter_suppresses_only_derived_margins() -> None:
    """A zero-revenue quarter drops gross_margin/operating_margin but keeps revenue."""
    source = FinancialMetricsSource()

    revenue = [0.0, 900.0, 800.0, 700.0]
    income_stmt = _full_income_stmt(revenue=revenue)

    with patch(
        "app.services.financial_metrics_source.yfinance.Ticker",
        new=_fake_ticker_factory(
            income_stmt=income_stmt,
            balance_sheet=_full_balance_sheet(),
            cashflow=_full_cashflow(),
        ),
    ):
        rows = await source.get_quarterly_metrics("AAPL")

    zero_period = "2024-03-31"

    revenue_rows = [
        row for row in rows if row["metric_name"] == METRIC_REVENUE and row["period"] == zero_period
    ]
    assert len(revenue_rows) == 1
    assert revenue_rows[0]["value"] == 0.0

    margin_rows = [
        row
        for row in rows
        if row["metric_name"] in (METRIC_GROSS_MARGIN, METRIC_OPERATING_MARGIN)
        and row["period"] == zero_period
    ]
    assert margin_rows == []

    # Other periods are unaffected.
    other_margin_rows = [
        row
        for row in rows
        if row["metric_name"] == METRIC_GROSS_MARGIN and row["period"] == "2023-12-31"
    ]
    assert len(other_margin_rows) == 1


# ---------------------------------------------------------------------------
# NaN / infinite guard
# ---------------------------------------------------------------------------


async def test_nan_value_produces_no_row() -> None:
    """A NaN figure produces no row for that metric/period pair."""
    source = FinancialMetricsSource()

    net_income = [float("nan"), 90.0, 80.0, 70.0]
    income_stmt = _full_income_stmt(net_income=net_income)

    with patch(
        "app.services.financial_metrics_source.yfinance.Ticker",
        new=_fake_ticker_factory(
            income_stmt=income_stmt,
            balance_sheet=_full_balance_sheet(),
            cashflow=_full_cashflow(),
        ),
    ):
        rows = await source.get_quarterly_metrics("AAPL")

    net_income_rows = [
        row
        for row in rows
        if row["metric_name"] == METRIC_NET_INCOME and row["period"] == "2024-03-31"
    ]
    assert net_income_rows == []

    # Every remaining value in the result is finite.
    assert all(math.isfinite(row["value"]) for row in rows)


async def test_infinite_value_produces_no_row() -> None:
    """An infinite figure produces no row for that metric/period pair."""
    source = FinancialMetricsSource()

    fcf = [150.0, float("inf"), 130.0, 120.0]
    cashflow = _full_cashflow(fcf=fcf)

    with patch(
        "app.services.financial_metrics_source.yfinance.Ticker",
        new=_fake_ticker_factory(
            income_stmt=_full_income_stmt(),
            balance_sheet=_full_balance_sheet(),
            cashflow=cashflow,
        ),
    ):
        rows = await source.get_quarterly_metrics("AAPL")

    fcf_rows = [
        row
        for row in rows
        if row["metric_name"] == METRIC_FREE_CASH_FLOW and row["period"] == "2023-12-31"
    ]
    assert fcf_rows == []

    # Every remaining value in the result is finite.
    assert all(math.isfinite(row["value"]) for row in rows)


# ---------------------------------------------------------------------------
# Per-statement resilience
# ---------------------------------------------------------------------------


async def test_raising_balance_sheet_fetch_still_yields_other_metrics() -> None:
    """A raising balance-sheet fetch still yields income-statement and cash-flow metrics."""
    source = FinancialMetricsSource()

    with patch(
        "app.services.financial_metrics_source.yfinance.Ticker",
        new=_fake_ticker_factory(
            income_stmt=_full_income_stmt(),
            cashflow=_full_cashflow(),
            raise_on={"balance_sheet"},
        ),
    ):
        rows = await source.get_quarterly_metrics("AAPL")

    produced_metric_names = {row["metric_name"] for row in rows}
    assert METRIC_DEBT_TO_EQUITY not in produced_metric_names
    assert METRIC_REVENUE in produced_metric_names
    assert METRIC_NET_INCOME in produced_metric_names
    assert METRIC_GROSS_MARGIN in produced_metric_names
    assert METRIC_OPERATING_MARGIN in produced_metric_names
    assert METRIC_FREE_CASH_FLOW in produced_metric_names


async def test_all_statement_fetches_raising_yields_empty_list() -> None:
    """All three statement fetches raising yields [] with no exception."""
    source = FinancialMetricsSource()

    with patch(
        "app.services.financial_metrics_source.yfinance.Ticker",
        new=_fake_ticker_factory(
            raise_on={"income_stmt", "balance_sheet", "cashflow"},
        ),
    ):
        rows = await source.get_quarterly_metrics("AAPL")

    assert rows == []


# ---------------------------------------------------------------------------
# Period shape
# ---------------------------------------------------------------------------


async def test_period_strings_match_iso_date_shape() -> None:
    """period values are ISO date strings of the form YYYY-MM-DD."""
    source = FinancialMetricsSource()

    with patch(
        "app.services.financial_metrics_source.yfinance.Ticker",
        new=_fake_ticker_factory(
            income_stmt=_full_income_stmt(),
            balance_sheet=_full_balance_sheet(),
            cashflow=_full_cashflow(),
        ),
    ):
        rows = await source.get_quarterly_metrics("AAPL")

    assert rows, "expected at least one row to validate period shape"
    for row in rows:
        assert _PERIOD_PATTERN.match(row["period"]), row["period"]
        assert row["period"] in _EXPECTED_PERIODS


# ---------------------------------------------------------------------------
# Alias label resolution
# ---------------------------------------------------------------------------


async def test_alternate_row_label_resolves() -> None:
    """An alternate row label from the alias tuples still resolves (Operating Revenue)."""
    source = FinancialMetricsSource()

    income_stmt = _full_income_stmt(revenue_label="Operating Revenue")

    with patch(
        "app.services.financial_metrics_source.yfinance.Ticker",
        new=_fake_ticker_factory(
            income_stmt=income_stmt,
            balance_sheet=_full_balance_sheet(),
            cashflow=_full_cashflow(),
        ),
    ):
        rows = await source.get_quarterly_metrics("AAPL")

    revenue_rows = [
        row
        for row in rows
        if row["metric_name"] == METRIC_REVENUE and row["period"] == "2024-03-31"
    ]
    assert len(revenue_rows) == 1
    assert revenue_rows[0]["value"] == pytest.approx(1000.0)
