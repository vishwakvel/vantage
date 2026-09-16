"""Quarterly fundamentals time-series client — yfinance-backed (D-01/D-02/D-03).

This module answers a *time-series* question — how has one company's own
fundamentals moved quarter over quarter — as distinct from
``comparables_source.py``, which answers a *cross-sectional* question (who
are this ticker's peers and how do they compare right now). METRIC-03's
"company's own historical range" requirement is exactly what this module
produces rows for.

yfinance imports are confined to ``app/services/`` — this module and
``app/services/comparables_source.py`` are the two files holding them,
enforced by ``tests/test_boundaries.py::test_yfinance_imports_confined_to_services``.
Agents import the module-level ``financial_metrics_source`` singleton below
and never import yfinance directly.

yfinance is synchronous/blocking under the hood. Every yfinance call is
offloaded to a worker thread via ``asyncio.to_thread`` so these async methods
never block the event loop while 5 agents fan out concurrently (T-09-DOS,
same mitigation ``comparables_source.py`` already applies).
"""

import asyncio
import math
from typing import Any

import yfinance

from app.services.api_call_counter import increment_api_call_count

# ---------------------------------------------------------------------------
# Metric-name constants — the canonical vocabulary written into
# FinancialMetric.metric_name (D-01).
# ---------------------------------------------------------------------------

METRIC_REVENUE = "revenue"
METRIC_NET_INCOME = "net_income"
METRIC_GROSS_MARGIN = "gross_margin"
METRIC_OPERATING_MARGIN = "operating_margin"
METRIC_DEBT_TO_EQUITY = "debt_to_equity"
METRIC_FREE_CASH_FLOW = "free_cash_flow"

CORE_METRICS: tuple[str, ...] = (
    METRIC_REVENUE,
    METRIC_NET_INCOME,
    METRIC_GROSS_MARGIN,
    METRIC_OPERATING_MARGIN,
    METRIC_DEBT_TO_EQUITY,
    METRIC_FREE_CASH_FLOW,
)

# ---------------------------------------------------------------------------
# Statement row-label aliases — yfinance's row labels drift between tickers
# and versions, so the first label present in a frame's index is resolved.
# ---------------------------------------------------------------------------

_TOTAL_REVENUE_LABELS: tuple[str, ...] = ("Total Revenue", "Operating Revenue")
_GROSS_PROFIT_LABELS: tuple[str, ...] = ("Gross Profit",)
_OPERATING_INCOME_LABELS: tuple[str, ...] = (
    "Operating Income",
    "Operating Income Or Loss",
)
_NET_INCOME_LABELS: tuple[str, ...] = (
    "Net Income",
    "Net Income Common Stockholders",
    "Net Income From Continuing Operation Net Minority Interest",
)
_TOTAL_DEBT_LABELS: tuple[str, ...] = ("Total Debt",)
_STOCKHOLDERS_EQUITY_LABELS: tuple[str, ...] = (
    "Stockholders Equity",
    "Total Stockholder Equity",
)
_FREE_CASH_FLOW_LABELS: tuple[str, ...] = ("Free Cash Flow",)


class FinancialMetricsSource:
    """Quarterly fundamentals client backed by yfinance.

    One public coroutine, ``get_quarterly_metrics``, fetches the three
    quarterly statement frames (income statement, balance sheet, cash flow)
    independently, each degrading to ``None`` on its own failure so one bad
    statement never suppresses the metrics derivable from the other two
    (T-09-PARTIAL). Never raises; returns ``[]`` when nothing could be
    derived.
    """

    async def get_quarterly_metrics(self, ticker: str) -> list[dict[str, Any]]:
        """Return flat quarterly metric rows for the six core metrics.

        Args:
            ticker: The subject ticker to fetch quarterly statements for.

        Returns:
            A list of ``{"metric_name": str, "period": str, "value": float}``
            dicts — the exact row shape plan 09-04's upsert consumes. Returns
            ``[]`` when every statement fetch fails or nothing could be
            derived; never raises.

        Increments the external-API call counter (OBS-02) once per
        underlying yfinance fetch — three increments per call, matching the
        three real outbound interactions this method performs — not once
        per method invocation. Each increment no-ops outside a research run
        (D-05).
        """
        try:
            await increment_api_call_count()
            income_stmt = await asyncio.to_thread(self._fetch_income_stmt, ticker)
        except Exception:
            income_stmt = None

        try:
            await increment_api_call_count()
            balance_sheet = await asyncio.to_thread(self._fetch_balance_sheet, ticker)
        except Exception:
            balance_sheet = None

        try:
            await increment_api_call_count()
            cashflow = await asyncio.to_thread(self._fetch_cashflow, ticker)
        except Exception:
            cashflow = None

        rows: list[dict[str, Any]] = []

        if income_stmt is not None:
            rows.extend(self._extract_income_metrics(income_stmt))

        if balance_sheet is not None:
            rows.extend(self._extract_balance_sheet_metrics(balance_sheet))

        if cashflow is not None:
            rows.extend(self._extract_cashflow_metrics(cashflow))

        return rows

    # -------------------------------------------------------------------
    # Per-statement extraction
    # -------------------------------------------------------------------

    def _extract_income_metrics(self, frame: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        revenue_row = self._resolve_row(frame, _TOTAL_REVENUE_LABELS)
        net_income_row = self._resolve_row(frame, _NET_INCOME_LABELS)
        gross_profit_row = self._resolve_row(frame, _GROSS_PROFIT_LABELS)
        operating_income_row = self._resolve_row(frame, _OPERATING_INCOME_LABELS)

        for column in frame.columns:
            period = self._period_from_column(column)
            if period is None:
                continue

            revenue_value = self._coerce_float(
                revenue_row[column] if revenue_row is not None else None
            )
            if revenue_value is not None:
                rows.append(
                    {
                        "metric_name": METRIC_REVENUE,
                        "period": period,
                        "value": revenue_value,
                    }
                )

            net_income_value = self._coerce_float(
                net_income_row[column] if net_income_row is not None else None
            )
            if net_income_value is not None:
                rows.append(
                    {
                        "metric_name": METRIC_NET_INCOME,
                        "period": period,
                        "value": net_income_value,
                    }
                )

            if revenue_value is not None and revenue_value != 0:
                gross_profit_value = self._coerce_float(
                    gross_profit_row[column] if gross_profit_row is not None else None
                )
                if gross_profit_value is not None:
                    rows.append(
                        {
                            "metric_name": METRIC_GROSS_MARGIN,
                            "period": period,
                            "value": gross_profit_value / revenue_value,
                        }
                    )

                operating_income_value = self._coerce_float(
                    operating_income_row[column] if operating_income_row is not None else None
                )
                if operating_income_value is not None:
                    rows.append(
                        {
                            "metric_name": METRIC_OPERATING_MARGIN,
                            "period": period,
                            "value": operating_income_value / revenue_value,
                        }
                    )

        return rows

    def _extract_balance_sheet_metrics(self, frame: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        total_debt_row = self._resolve_row(frame, _TOTAL_DEBT_LABELS)
        equity_row = self._resolve_row(frame, _STOCKHOLDERS_EQUITY_LABELS)

        if total_debt_row is None or equity_row is None:
            return rows

        for column in frame.columns:
            period = self._period_from_column(column)
            if period is None:
                continue

            equity_value = self._coerce_float(equity_row[column])
            if equity_value is None or equity_value == 0:
                continue

            debt_value = self._coerce_float(total_debt_row[column])
            if debt_value is None:
                continue

            rows.append(
                {
                    "metric_name": METRIC_DEBT_TO_EQUITY,
                    "period": period,
                    "value": debt_value / equity_value,
                }
            )

        return rows

    def _extract_cashflow_metrics(self, frame: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        fcf_row = self._resolve_row(frame, _FREE_CASH_FLOW_LABELS)
        if fcf_row is None:
            return rows

        for column in frame.columns:
            period = self._period_from_column(column)
            if period is None:
                continue

            fcf_value = self._coerce_float(fcf_row[column])
            if fcf_value is None:
                continue

            rows.append(
                {
                    "metric_name": METRIC_FREE_CASH_FLOW,
                    "period": period,
                    "value": fcf_value,
                }
            )

        return rows

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _resolve_row(frame: Any, labels: tuple[str, ...]) -> Any:
        """Return the frame row for the first alias present in *labels*, or None."""
        for label in labels:
            if label in frame.index:
                return frame.loc[label]
        return None

    @staticmethod
    def _period_from_column(column: Any) -> str | None:
        """Convert a quarter-end column label to a YYYY-MM-DD string, or None."""
        if hasattr(column, "date"):
            period = column.date().isoformat()
        else:
            period = str(column)[:10]

        if len(period) == 10 and period[4] == "-" and period[7] == "-":
            return period
        return None

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        """Return *value* as a finite float, or None if not finite/numeric."""
        try:
            float_value = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(float_value):
            return None
        return float_value

    # -------------------------------------------------------------------
    # Blocking-call isolation
    # -------------------------------------------------------------------

    @staticmethod
    def _fetch_income_stmt(ticker: str) -> Any:
        """Blocking yfinance call — always invoke via asyncio.to_thread."""
        return yfinance.Ticker(ticker).quarterly_income_stmt

    @staticmethod
    def _fetch_balance_sheet(ticker: str) -> Any:
        """Blocking yfinance call — always invoke via asyncio.to_thread."""
        return yfinance.Ticker(ticker).quarterly_balance_sheet

    @staticmethod
    def _fetch_cashflow(ticker: str) -> Any:
        """Blocking yfinance call — always invoke via asyncio.to_thread."""
        return yfinance.Ticker(ticker).quarterly_cashflow


# ---------------------------------------------------------------------------
# Module-level singleton — import this; do NOT create additional instances
# ---------------------------------------------------------------------------

financial_metrics_source: FinancialMetricsSource = FinancialMetricsSource()
