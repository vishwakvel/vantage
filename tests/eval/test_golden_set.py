"""Tests for app.eval.golden_set (D-07, OBS-03).

Tests not selected by `-k "not shipped"` (i.e. matching "shipped") assert
against the actual shipped `ragas_golden_set.json` fixture and are added in
Task 2, once that fixture exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval.golden_set import (
    ALLOWED_PROVENANCE,
    MIN_REFERENCE_CONTEXT_CHARS,
    GoldenCase,
    GoldenSetError,
    load_golden_set,
)

# A passage safely over MIN_REFERENCE_CONTEXT_CHARS, used across multiple
# well-formed test fixtures below.
_LONG_PASSAGE = (
    "The Company's business is subject to a variety of risks, including "
    "risks related to global supply chain concentration, fluctuations in "
    "component pricing, and dependence on a limited number of suppliers "
    "for certain critical parts used in its products. Any disruption to "
    "these supplier relationships could materially affect the Company's "
    "ability to meet customer demand in a timely manner."
)


def _well_formed_case(case_id: str = "case-1", ticker: str = "AAPL") -> dict:
    return {
        "case_id": case_id,
        "query": "What supply-chain concentration risks does the company disclose?",
        "ticker": ticker,
        "user_id": "",
        "provenance": "authored",
        "reference_contexts": [_LONG_PASSAGE],
    }


def _write_fixture(tmp_path: Path, cases: list[dict], filename: str = "golden.json") -> Path:
    path = tmp_path / filename
    path.write_text(json.dumps(cases), encoding="utf-8")
    return path


def _well_formed_cases(count: int = 10) -> list[dict]:
    tickers = ["AAPL", "MSFT", "TSLA"]
    return [
        _well_formed_case(case_id=f"case-{i}", ticker=tickers[i % len(tickers)])
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Test 1: no-argument load reads the shipped fixture, returns list[GoldenCase]
# ---------------------------------------------------------------------------


def test_load_golden_set_no_argument_returns_golden_cases_shipped():
    cases = load_golden_set()
    assert isinstance(cases, list)
    assert len(cases) > 0
    assert all(isinstance(case, GoldenCase) for case in cases)


# ---------------------------------------------------------------------------
# Test 2: too-short reference_contexts entry raises, naming the case_id
# ---------------------------------------------------------------------------


def test_short_reference_context_raises_naming_case_id(tmp_path: Path):
    cases = _well_formed_cases(10)
    cases[3]["case_id"] = "short-passage-case"
    cases[3]["reference_contexts"] = ["too short"]
    path = _write_fixture(tmp_path, cases)

    with pytest.raises(GoldenSetError) as exc_info:
        load_golden_set(path=path)

    assert "short-passage-case" in str(exc_info.value)
    assert str(MIN_REFERENCE_CONTEXT_CHARS) in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 3: missing required key raises, naming the key and case index
# ---------------------------------------------------------------------------


def test_missing_required_key_raises_naming_key_and_index(tmp_path: Path):
    cases = _well_formed_cases(10)
    del cases[2]["provenance"]
    path = _write_fixture(tmp_path, cases)

    with pytest.raises(GoldenSetError) as exc_info:
        load_golden_set(path=path)

    message = str(exc_info.value)
    assert "provenance" in message
    assert "index 2" in message


# ---------------------------------------------------------------------------
# Test 4: top level not a JSON list raises
# ---------------------------------------------------------------------------


def test_top_level_not_a_list_raises(tmp_path: Path):
    path = tmp_path / "golden.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

    with pytest.raises(GoldenSetError):
        load_golden_set(path=path)


# ---------------------------------------------------------------------------
# Test 5: duplicate case_id values raise
# ---------------------------------------------------------------------------


def test_duplicate_case_id_raises(tmp_path: Path):
    cases = _well_formed_cases(10)
    cases[1]["case_id"] = cases[0]["case_id"]
    path = _write_fixture(tmp_path, cases)

    with pytest.raises(GoldenSetError) as exc_info:
        load_golden_set(path=path)

    assert cases[0]["case_id"] in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 6: empty reference_contexts list raises
# ---------------------------------------------------------------------------


def test_empty_reference_contexts_raises(tmp_path: Path):
    cases = _well_formed_cases(10)
    cases[0]["reference_contexts"] = []
    path = _write_fixture(tmp_path, cases)

    with pytest.raises(GoldenSetError):
        load_golden_set(path=path)


# ---------------------------------------------------------------------------
# Test 7: GoldenSetError reports ALL offending cases, not just the first
# ---------------------------------------------------------------------------


def test_error_reports_all_offending_cases(tmp_path: Path):
    cases = _well_formed_cases(10)
    cases[0]["case_id"] = "bad-short"
    cases[0]["reference_contexts"] = ["too short"]
    cases[5]["case_id"] = "bad-missing-key"
    del cases[5]["ticker"]
    path = _write_fixture(tmp_path, cases)

    with pytest.raises(GoldenSetError) as exc_info:
        load_golden_set(path=path)

    message = str(exc_info.value)
    assert "bad-short" in message
    assert "index 5" in message
    assert "ticker" in message


# ---------------------------------------------------------------------------
# Test 8: well-formed in-test fixture loads via explicit path argument
# ---------------------------------------------------------------------------


def test_well_formed_fixture_loads_via_explicit_path(tmp_path: Path):
    cases = _well_formed_cases(10)
    path = _write_fixture(tmp_path, cases)

    loaded = load_golden_set(path=path)

    assert len(loaded) == 10
    assert all(isinstance(case, GoldenCase) for case in loaded)
    assert loaded[0].case_id == "case-0"
    assert loaded[0].user_id == ""
    assert loaded[0].reference_contexts == (_LONG_PASSAGE,)


# ---------------------------------------------------------------------------
# Shipped-fixture test: locks the shape of the real ragas_golden_set.json in.
# Named to match the "-k shipped" selector Task 1's verify command excluded.
# ---------------------------------------------------------------------------


def test_shipped_golden_set_has_ten_to_twenty_cases_across_three_tickers():
    cases = load_golden_set()

    assert 10 <= len(cases) <= 20
    assert sorted({case.ticker for case in cases}) == ["AAPL", "MSFT", "TSLA"]


def test_shipped_golden_set_every_case_has_reference_context():
    cases = load_golden_set()

    assert len(cases) > 0
    for case in cases:
        assert len(case.reference_contexts) >= 1
        for context in case.reference_contexts:
            assert len(context) >= MIN_REFERENCE_CONTEXT_CHARS


def test_shipped_golden_set_every_provenance_value_is_allowed():
    cases = load_golden_set()

    assert len(cases) > 0
    for case in cases:
        assert case.provenance in ALLOWED_PROVENANCE
