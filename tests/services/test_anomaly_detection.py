"""Unit tests for ``detect_anomalies`` (09-05-PLAN.md, METRIC-03).

Coverage:
  - A six-quarter series whose final value is a clear outlier against the
    preceding five yields exactly one anomaly, for that final period.
  - A three-quarter series yields no anomalies and is listed in
    ``skipped_metrics``; a four-quarter series is eligible and is absent
    from ``skipped_metrics`` (the D-04 boundary, tested on both sides).
  - A series whose values are all identical yields no anomalies and is not
    reported as skipped.
  - An empty input mapping yields an empty report.
  - Every returned anomaly's ``severity`` is one of exactly High/Medium/Low.
  - Calling the function twice with the same input returns equal reports
    (T-09-NONDET determinism).
  - Description formatting: ``gross_margin`` renders a one-decimal
    percentage, ``debt_to_equity`` a two-decimal plain number, ``revenue``
    a compact currency form.
  - An anomaly at the series minimum states it is the lowest on record; one
    at the maximum states it is the highest.
  - A short metric being skipped does not affect detection on a longer
    metric evaluated in the same call.
  - Every anomaly dict survives ``json.dumps`` with no custom encoder
    (T-09-NUMPY — no numpy scalar leaks into the payload).

These are pure-function tests: no ``db_session``, no ``anyio`` marker, no
mocks — the module has no I/O.
"""

import json

from app.services.anomaly_detection import MIN_QUARTERS, AnomalyReport, detect_anomalies

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Six tight quarters followed by one order-of-magnitude outlier in the
# final quarter — an unambiguous break, not a value tuned against the
# model's internals.
_REVENUE_WITH_OUTLIER: list[tuple[str, float]] = [
    ("2023-06-30", 100.0),
    ("2023-09-30", 102.0),
    ("2023-12-31", 98.0),
    ("2024-03-31", 101.0),
    ("2024-06-30", 99.0),
    ("2024-09-30", 500.0),
]

_DEBT_TO_EQUITY_WITH_OUTLIER: list[tuple[str, float]] = [
    ("2023-06-30", 0.50),
    ("2023-09-30", 0.52),
    ("2023-12-31", 0.48),
    ("2024-03-31", 0.51),
    ("2024-06-30", 0.49),
    ("2024-09-30", 4.00),
]

_GROSS_MARGIN_WITH_LOW_OUTLIER: list[tuple[str, float]] = [
    ("2024-03-31", 0.40),
    ("2024-06-30", 0.41),
    ("2024-09-30", 0.39),
    ("2024-12-31", 0.10),
    ("2025-03-31", 0.42),
    ("2025-06-30", 0.395),
]

_THREE_QUARTERS: list[tuple[str, float]] = [
    ("2024-03-31", 10.0),
    ("2024-06-30", 11.0),
    ("2024-09-30", 9.0),
]

_FLAT_SERIES: list[tuple[str, float]] = [
    ("2024-03-31", 100.0),
    ("2024-06-30", 100.0),
    ("2024-09-30", 100.0),
    ("2024-12-31", 100.0),
    ("2025-03-31", 100.0),
]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_six_quarter_series_flags_exactly_one_anomaly_at_final_period() -> None:
    """A clear final-quarter outlier yields exactly one anomaly, for that period."""
    report = detect_anomalies({"revenue": _REVENUE_WITH_OUTLIER})

    assert len(report.anomalies) == 1
    assert report.anomalies[0]["period"] == "2024-09-30"
    assert report.anomalies[0]["metric_name"] == "revenue"


def test_empty_input_mapping_yields_empty_report() -> None:
    """An empty input mapping produces an empty anomalies list and skipped list."""
    report = detect_anomalies({})

    assert report == AnomalyReport(anomalies=[], skipped_metrics=[])


def test_short_metric_skipped_does_not_affect_long_metric_detection() -> None:
    """A too-short metric is skipped while a longer metric is still evaluated."""
    report = detect_anomalies(
        {
            "net_income": _THREE_QUARTERS,
            "revenue": _REVENUE_WITH_OUTLIER,
        }
    )

    assert "net_income" in report.skipped_metrics
    assert "revenue" not in report.skipped_metrics
    assert any(a["metric_name"] == "revenue" for a in report.anomalies)
    assert not any(a["metric_name"] == "net_income" for a in report.anomalies)


# ---------------------------------------------------------------------------
# MIN_QUARTERS boundary (D-04)
# ---------------------------------------------------------------------------


def test_three_quarter_series_is_skipped_not_flagged() -> None:
    """Fewer than MIN_QUARTERS points: no anomalies, metric listed as skipped."""
    report = detect_anomalies({"net_income": _THREE_QUARTERS})

    assert report.anomalies == []
    assert report.skipped_metrics == ["net_income"]


def test_four_quarter_series_is_eligible_and_not_skipped() -> None:
    """Exactly MIN_QUARTERS points: metric is eligible, absent from skipped_metrics."""
    assert MIN_QUARTERS == 4
    series = _GROSS_MARGIN_WITH_LOW_OUTLIER[:4]

    report = detect_anomalies({"gross_margin": series})

    assert "gross_margin" not in report.skipped_metrics


# ---------------------------------------------------------------------------
# Flat series
# ---------------------------------------------------------------------------


def test_all_identical_series_yields_no_anomalies_and_is_not_skipped() -> None:
    """A flat series has no range to be an outlier against — not skipped either."""
    report = detect_anomalies({"revenue": _FLAT_SERIES})

    assert report.anomalies == []
    assert report.skipped_metrics == []


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


def test_every_anomaly_severity_is_one_of_the_three_allowed_strings() -> None:
    """severity is always a member of {High, Medium, Low}."""
    report = detect_anomalies(
        {
            "revenue": _REVENUE_WITH_OUTLIER,
            "debt_to_equity": _DEBT_TO_EQUITY_WITH_OUTLIER,
            "gross_margin": _GROSS_MARGIN_WITH_LOW_OUTLIER,
        }
    )

    assert report.anomalies, "expected at least one anomaly across these fixtures"
    for anomaly in report.anomalies:
        assert anomaly["severity"] in {"High", "Medium", "Low"}


# ---------------------------------------------------------------------------
# Determinism (T-09-NONDET)
# ---------------------------------------------------------------------------


def test_two_calls_on_identical_input_return_equal_reports() -> None:
    """random_state is pinned: re-running on unchanged data is byte-identical."""
    series_by_metric = {
        "revenue": _REVENUE_WITH_OUTLIER,
        "debt_to_equity": _DEBT_TO_EQUITY_WITH_OUTLIER,
        "gross_margin": _GROSS_MARGIN_WITH_LOW_OUTLIER,
        "net_income": _THREE_QUARTERS,
    }

    first = detect_anomalies(series_by_metric)
    second = detect_anomalies(series_by_metric)

    assert first == second


# ---------------------------------------------------------------------------
# Description formatting
# ---------------------------------------------------------------------------


def test_gross_margin_description_renders_one_decimal_percentage() -> None:
    """A gross_margin anomaly description contains a one-decimal percentage."""
    report = detect_anomalies({"gross_margin": _GROSS_MARGIN_WITH_LOW_OUTLIER})

    anomaly = next(a for a in report.anomalies if a["metric_name"] == "gross_margin")
    assert "%" in anomaly["description"]
    # one-decimal percentage of the flagged value, e.g. "10.0%"
    expected_fragment = f"{anomaly['value'] * 100:.1f}%"
    assert expected_fragment in anomaly["description"]


def test_debt_to_equity_description_renders_two_decimal_plain_number() -> None:
    """A debt_to_equity anomaly description contains a two-decimal plain number."""
    report = detect_anomalies({"debt_to_equity": _DEBT_TO_EQUITY_WITH_OUTLIER})

    anomaly = next(a for a in report.anomalies if a["metric_name"] == "debt_to_equity")
    expected_fragment = f"{anomaly['value']:.2f}"
    assert expected_fragment in anomaly["description"]
    assert "%" not in anomaly["description"]
    assert "$" not in anomaly["description"]


def test_revenue_description_renders_compact_currency_form() -> None:
    """A revenue anomaly description contains a compact currency form."""
    report = detect_anomalies({"revenue": _REVENUE_WITH_OUTLIER})

    anomaly = next(a for a in report.anomalies if a["metric_name"] == "revenue")
    assert "$" in anomaly["description"]


# ---------------------------------------------------------------------------
# Position in series (lowest / highest)
# ---------------------------------------------------------------------------


def test_minimum_value_anomaly_states_it_is_lowest_on_record() -> None:
    """An anomaly at the series minimum describes itself as the lowest on record."""
    report = detect_anomalies({"gross_margin": _GROSS_MARGIN_WITH_LOW_OUTLIER})

    anomaly = next(a for a in report.anomalies if a["metric_name"] == "gross_margin")
    assert anomaly["value"] == min(v for _, v in _GROSS_MARGIN_WITH_LOW_OUTLIER)
    assert "lowest" in anomaly["description"]


def test_maximum_value_anomaly_states_it_is_highest_on_record() -> None:
    """An anomaly at the series maximum describes itself as the highest on record."""
    report = detect_anomalies({"debt_to_equity": _DEBT_TO_EQUITY_WITH_OUTLIER})

    anomaly = next(a for a in report.anomalies if a["metric_name"] == "debt_to_equity")
    assert anomaly["value"] == max(v for _, v in _DEBT_TO_EQUITY_WITH_OUTLIER)
    assert "highest" in anomaly["description"]


# ---------------------------------------------------------------------------
# JSON serialisability (T-09-NUMPY)
# ---------------------------------------------------------------------------


def test_anomalies_survive_json_dumps_with_no_custom_encoder() -> None:
    """No numpy scalar leaks into the anomaly dicts written to AgentOutput.output."""
    report = detect_anomalies(
        {
            "revenue": _REVENUE_WITH_OUTLIER,
            "debt_to_equity": _DEBT_TO_EQUITY_WITH_OUTLIER,
        }
    )

    assert report.anomalies, "expected at least one anomaly across these fixtures"
    json.dumps(report.anomalies)
