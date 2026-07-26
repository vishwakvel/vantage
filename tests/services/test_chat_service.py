"""Unit tests for ``app.services.chat_service`` (08-02-PLAN.md).

Coverage (CHAT-02, CHAT-04):
  - test_split_narrative_and_json_*: fenced-JSON-after-narrative split,
    mirrors ``app.agents.synthesis._split_narrative_and_json`` exactly.
  - test_parse_coverage_flag_*: never-raise parse/repair/validate pipeline,
    default direction False (assume covered) on any failure.
  - test_serialize_memo_*: D-10 full-body grounding (all 5 specialist
    sections + synthesis.take + contradictions), None-guarded per section.
  - test_serialize_history_*: D-11 last-8-message sliding window,
    oldest-first, role-labeled.

Mocks only at the SERVICE boundary — ``app.services.chat_service.call_groq``
— never the groq SDK directly (mirrors ``tests/agents/test_synthesis.py``'s
boundary-mock convention). Pure functions here run with no DB and no
network under plain ``pytest`` (not `-m live`).
"""

from __future__ import annotations

from typing import Any

from app.services.chat_service import (
    _HISTORY_WINDOW,
    _parse_coverage_flag,
    _serialize_history,
    _serialize_memo,
    _split_narrative_and_json,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _FakeMessage:
    """Minimal stand-in for a ``ChatMessage`` row — only ``.role``/``.content``
    are read by ``_serialize_history``."""

    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


def _full_memo_body() -> dict[str, Any]:
    """A complete memo body across all 5 specialist sections + synthesis,
    matching the shape ``app/workers/tasks.py::_run_research_async``
    assembles onto ``ResearchMemo.body``."""
    return {
        "fundamentals": {"narrative": "Revenue grew 12% YoY.", "section": "fundamentals"},
        "sentiment": {"narrative": "News sentiment is broadly positive.", "section": "sentiment"},
        "risks": {"narrative": "Regulatory risk in the EU market.", "section": "risks"},
        "macro": {"narrative": "Rate environment is a tailwind.", "section": "macro"},
        "comparables": {"narrative": "Trades at a discount to peers.", "section": "comparables"},
        "synthesis": {
            "take": "Overall a buy given the growth and valuation picture.",
            "section": "synthesis",
            "contradictions": [
                {
                    "topic": "Growth outlook",
                    "agents": ["FundamentalAnalysis", "RiskAssessment"],
                    "description": "Fundamentals sees growth; Risk flags EU regulatory drag.",
                    "severity": "Medium",
                }
            ],
        },
    }


# ---------------------------------------------------------------------------
# _split_narrative_and_json
# ---------------------------------------------------------------------------


def test_split_narrative_and_json_with_fence() -> None:
    raw = 'answer\n```json\n{"coverage_exceeded": true}\n```'
    narrative, fenced = _split_narrative_and_json(raw)
    assert narrative == "answer"
    assert fenced == '{"coverage_exceeded": true}'


def test_split_narrative_and_json_no_fence() -> None:
    raw = "just an answer, no fence"
    narrative, fenced = _split_narrative_and_json(raw)
    assert narrative == "just an answer, no fence"
    assert fenced is None


def test_split_narrative_and_json_non_string_input() -> None:
    narrative, fenced = _split_narrative_and_json(None)  # type: ignore[arg-type]
    assert narrative == ""
    assert fenced is None


# ---------------------------------------------------------------------------
# _parse_coverage_flag
# ---------------------------------------------------------------------------


def test_parse_coverage_flag_none() -> None:
    assert _parse_coverage_flag(None) is False


def test_parse_coverage_flag_empty_string() -> None:
    assert _parse_coverage_flag("") is False


def test_parse_coverage_flag_true() -> None:
    assert _parse_coverage_flag('{"coverage_exceeded": true}') is True


def test_parse_coverage_flag_false() -> None:
    assert _parse_coverage_flag('{"coverage_exceeded": false}') is False


def test_parse_coverage_flag_repairable_trailing_comma() -> None:
    assert _parse_coverage_flag('{"coverage_exceeded": true,}') is True


def test_parse_coverage_flag_repairable_single_quotes() -> None:
    assert _parse_coverage_flag("{'coverage_exceeded': false}") is False


def test_parse_coverage_flag_unrecoverable_garbage() -> None:
    assert _parse_coverage_flag("not json at all {{{") is False


# ---------------------------------------------------------------------------
# _serialize_memo
# ---------------------------------------------------------------------------


def test_serialize_memo_includes_all_five_sections_plus_take_and_contradiction() -> None:
    text = _serialize_memo(_full_memo_body())
    assert "Revenue grew 12% YoY." in text
    assert "News sentiment is broadly positive." in text
    assert "Regulatory risk in the EU market." in text
    assert "Rate environment is a tailwind." in text
    assert "Trades at a discount to peers." in text
    assert "Overall a buy given the growth and valuation picture." in text
    assert "Growth outlook" in text


def test_serialize_memo_none_guards_failed_section() -> None:
    body = _full_memo_body()
    body["risks"] = {"narrative": None, "status": "FAILED", "reason": "Risk unavailable"}
    text = _serialize_memo(body)
    # Does not raise, and the other sections still serialize.
    assert "Revenue grew 12% YoY." in text
    assert "Overall a buy given the growth and valuation picture." in text


# ---------------------------------------------------------------------------
# _serialize_history
# ---------------------------------------------------------------------------


def test_serialize_history_caps_to_last_eight_oldest_first() -> None:
    messages = [
        _FakeMessage("user" if i % 2 == 0 else "assistant", f"message {i}")
        for i in range(12)
    ]
    text = _serialize_history(messages)
    lines = text.split("\n")
    assert len(lines) == _HISTORY_WINDOW
    assert lines[0] == "user: message 4"
    assert lines[-1] == "assistant: message 11"
