"""Per-metric isolation-forest anomaly detection (METRIC-03).

Each of a company's six core financial metrics (see
``app/services/financial_metrics_source.py::CORE_METRICS``) is fitted
independently against that company's own quarterly history — never jointly
across metrics (R-C, 09-05-PLAN.md): a joint fit cannot attribute a flagged
point to a single named metric (METRIC-02), and with only four to eight
quarters a six-feature joint vector has more features than samples, which
degenerates the forest. A per-metric fit also tolerates a metric being
missing for some quarters without invalidating detection on the others,
which matters because free-tier yfinance data frequently has gaps.

A minimum of four quarters of history is required before any metric is
fitted (D-04): free-tier yfinance typically returns four to five quarters,
so an eight-quarter bar would silently disable this feature for most real
tickers. Metrics that fall short of the bar are never silently dropped —
they are named in ``AnomalyReport.skipped_metrics`` (D-05), the same
no-silent-gaps convention Phase 7 established for the empty-contradictions
panel.

Severity is bucketed from the forest's own ``decision_function`` score into
the same three-tier High/Medium/Low convention Contradictions established in
Phase 7 (D-08), reusing the exact ``SEVERITY_BADGES`` keys in
``frontend/src/labels.ts``. Descriptions are built here, in code, directly
from the stored numeric values — never routed through an LLM (D-10) — so a
flagged anomaly is byte-for-byte reproducible and cannot restate a number
slightly wrong.

This module is synchronous and CPU-bound by design: a forest fit blocks the
interpreter while it runs, so ``detect_anomalies`` is a plain function with
no I/O, no session, and no ``await``. The caller (plan 09-06) offloads it
via ``asyncio.to_thread`` so it never blocks the event loop during the
five-agent fan-out (T-09-DOS).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

import numpy
from sklearn.ensemble import IsolationForest

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# D-04: close to "whatever yfinance's free tier actually returns"
# (typically ~4-5 quarters) — an 8-quarter minimum would silently disable
# anomaly detection for most real tickers.
MIN_QUARTERS: int = 4

# Severity tiers bucketed from IsolationForest.decision_function, which is
# centred at zero by the contamination offset. Named constants so
# recalibration is a one-line change in this file (D-08).
_SEVERITY_HIGH_THRESHOLD: float = -0.15
_SEVERITY_MEDIUM_THRESHOLD: float = -0.05

# random_state is pinned so re-running detection on unchanged data yields
# byte-identical anomalies (T-09-NONDET) — a financial feature whose flagged
# anomalies shuffle between runs on the same numbers is not credible.
_N_ESTIMATORS: int = 100
_RANDOM_STATE: int = 42

_METRIC_LABELS: dict[str, str] = {
    "revenue": "Revenue",
    "net_income": "Net income",
    "gross_margin": "Gross margin",
    "operating_margin": "Operating margin",
    "debt_to_equity": "Debt-to-equity",
    "free_cash_flow": "Free cash flow",
}

# Formatted as a one-decimal percentage.
_RATIO_METRICS: frozenset[str] = frozenset({"gross_margin", "operating_margin"})

# Formatted as a plain two-decimal number. Everything else (revenue,
# net_income, free_cash_flow, and any unrecognized metric) formats as
# compact currency.
_PLAIN_RATIO_METRICS: frozenset[str] = frozenset({"debt_to_equity"})


@dataclass(frozen=True)
class AnomalyReport:
    """Result of a `detect_anomalies` call.

    Frozen because this report is a value returned to a node that only
    reads it. Two fields rather than a bare anomalies list so D-05's
    insufficient-history case is carried explicitly instead of being
    indistinguishable from "no anomalies found" — the same no-silent-gaps
    rule Phase 7's empty-contradictions panel established.
    """

    anomalies: list[dict[str, Any]]
    skipped_metrics: list[str]


def _label_for(metric_name: str) -> str:
    """Display label for a metric name, falling back to a humanized form."""
    if metric_name in _METRIC_LABELS:
        return _METRIC_LABELS[metric_name]
    return metric_name.replace("_", " ").capitalize()


def _format_percentage(value: float) -> str:
    """One-decimal percentage, e.g. 0.183 -> "18.3%"."""
    return f"{value * 100:.1f}%"


def _format_plain(value: float) -> str:
    """Plain two-decimal number, e.g. 0.5 -> "0.50"."""
    return f"{value:.2f}"


def _format_currency(value: float) -> str:
    """Compact currency form, e.g. 1_234_000_000 -> "$1.23B", sign preserved."""
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1_000_000_000:
        return f"{sign}${magnitude / 1_000_000_000:.2f}B"
    if magnitude >= 1_000_000:
        return f"{sign}${magnitude / 1_000_000:.1f}M"
    if magnitude >= 1_000:
        return f"{sign}${magnitude / 1_000:.1f}K"
    return f"{sign}${magnitude:.2f}"


def _format_value(metric_name: str, value: float) -> str:
    """Format a value for its metric family (ratio, plain-ratio, or currency)."""
    if metric_name in _RATIO_METRICS:
        return _format_percentage(value)
    if metric_name in _PLAIN_RATIO_METRICS:
        return _format_plain(value)
    return _format_currency(value)


def _severity_for(score: float) -> str:
    """Bucket a decision_function score into High/Medium/Low (D-08)."""
    if score <= _SEVERITY_HIGH_THRESHOLD:
        return "High"
    if score <= _SEVERITY_MEDIUM_THRESHOLD:
        return "Medium"
    return "Low"


def _describe(metric_name: str, period: str, value: float, values: list[float]) -> str:
    """Build one deterministic, investor-facing sentence from stored numbers alone.

    Contains no score values, no model internals, and no hedging language —
    only the metric's display label, direction relative to the series
    median, the formatted value, the period, its position in the series
    (lowest / highest / an outlier), and the series median (D-10).
    """
    label = _label_for(metric_name)
    median = statistics.median(values)
    direction = "dropped to" if value < median else "climbed to"
    formatted_value = _format_value(metric_name, value)
    formatted_median = _format_value(metric_name, median)
    n = len(values)

    if value == min(values):
        position = f"the lowest of the {n} quarters on record"
    elif value == max(values):
        position = f"the highest of the {n} quarters on record"
    else:
        position = f"an outlier against the {n} quarters on record"

    return (
        f"{label} {direction} {formatted_value} in {period}, {position} "
        f"(median {formatted_median})."
    )


def detect_anomalies(series_by_metric: dict[str, list[tuple[str, float]]]) -> AnomalyReport:
    """Fit an isolation forest per metric against that metric's own history.

    Iterates metrics in sorted name order so output ordering is stable.
    Each series is sorted by period ascending before use; the caller's
    ordering is never assumed. A series shorter than `MIN_QUARTERS` is
    recorded in `skipped_metrics` and never fitted (D-04/D-05). An
    all-identical series has no historical range to be an outlier against,
    so it is skipped without fitting and without being marked as skipped —
    it was not skipped for lack of history. Never raises on well-formed
    input.
    """
    anomalies: list[dict[str, Any]] = []
    skipped_metrics: list[str] = []

    for metric_name in sorted(series_by_metric):
        series = sorted(series_by_metric[metric_name], key=lambda item: item[0])

        if len(series) < MIN_QUARTERS:
            skipped_metrics.append(metric_name)
            continue

        values = [value for _, value in series]

        if len(set(values)) == 1:
            continue

        array = numpy.asarray(values, dtype=float).reshape(-1, 1)
        forest = IsolationForest(
            n_estimators=_N_ESTIMATORS,
            contamination="auto",
            random_state=_RANDOM_STATE,
        )
        forest.fit(array)
        predictions = forest.predict(array)
        scores = forest.decision_function(array)

        for (period, value), prediction, score in zip(series, predictions, scores, strict=True):
            if prediction != -1:
                continue
            anomalies.append(
                {
                    "metric_name": metric_name,
                    "period": period,
                    "value": float(value),
                    "severity": _severity_for(float(score)),
                    "description": _describe(metric_name, period, float(value), values),
                }
            )

    return AnomalyReport(anomalies=anomalies, skipped_metrics=skipped_metrics)
