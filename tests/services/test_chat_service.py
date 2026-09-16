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
  - test_build_chat_prompt_*: assembly order (framing, memo, history,
    question, coverage instruction last).
  - test_answer_chat_turn_*: single rate-limited ``call_groq`` call ->
    ``(narrative, coverage_exceeded)``, never raises.

Mocks only at the SERVICE boundary — ``app.services.chat_service.call_groq``
— never the groq SDK directly (mirrors ``tests/agents/test_synthesis.py``'s
boundary-mock convention). Pure functions here run with no DB and no
network under plain ``pytest`` (not `-m live`).
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import app.services.chat_service as chat_service
from app.services.chat_service import (
    _HISTORY_WINDOW,
    _build_chat_prompt,
    _parse_coverage_flag,
    _serialize_history,
    _serialize_memo,
    _split_narrative_and_json,
    answer_chat_turn,
)
from app.services.groq_client import GroqResult


#: Minimal GroqResult factory for mocking call_groq's new return contract —
#: only ``text`` matters to chat_service (it never persists token counts).
def _groq_result(text: str) -> GroqResult:
    return GroqResult(
        text=text,
        prompt_tokens=20,
        completion_tokens=15,
        usage_metadata={"input_tokens": 20, "output_tokens": 15, "total_tokens": 35},
    )


#: Runs the async tests in this module under anyio (asyncio backend, per
#: conftest.py's anyio_backend fixture) — mirrors
#: tests/agents/test_synthesis.py's module-level marker.
pytestmark = pytest.mark.anyio

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
        _FakeMessage("user" if i % 2 == 0 else "assistant", f"message {i}") for i in range(12)
    ]
    text = _serialize_history(messages)
    lines = text.split("\n")
    assert len(lines) == _HISTORY_WINDOW
    assert lines[0] == "user: message 4"
    assert lines[-1] == "assistant: message 11"


# ---------------------------------------------------------------------------
# _build_chat_prompt
# ---------------------------------------------------------------------------


def test_build_chat_prompt_assembly_order() -> None:
    body = _full_memo_body()
    history = [_FakeMessage("user", "What about margins?")]
    prompt = _build_chat_prompt(body, history, "What is the growth outlook?")

    framing_idx = prompt.find("DATA")
    memo_idx = prompt.find("Revenue grew 12% YoY.")
    history_idx = prompt.find("What about margins?")
    question_idx = prompt.find("What is the growth outlook?")
    coverage_idx = prompt.find("coverage_exceeded")

    assert -1 < framing_idx < memo_idx < history_idx < question_idx < coverage_idx


# ---------------------------------------------------------------------------
# answer_chat_turn
# ---------------------------------------------------------------------------


async def test_answer_chat_turn_returns_narrative_and_false_flag() -> None:
    body = _full_memo_body()
    with patch(
        "app.services.chat_service.call_groq",
        AsyncMock(
            return_value=_groq_result(
                'Grounded answer.\n```json\n{"coverage_exceeded": false}\n```'
            )
        ),
    ) as mock_call:
        narrative, coverage_exceeded = await answer_chat_turn(body, [], "A question?")

    assert narrative == "Grounded answer."
    assert coverage_exceeded is False
    mock_call.assert_awaited_once()
    assert mock_call.await_args.kwargs.get("max_tokens") == 1024


async def test_answer_chat_turn_returns_true_flag() -> None:
    body = _full_memo_body()
    with patch(
        "app.services.chat_service.call_groq",
        AsyncMock(
            return_value=_groq_result('Answer here.\n```json\n{"coverage_exceeded": true}\n```')
        ),
    ):
        narrative, coverage_exceeded = await answer_chat_turn(body, [], "A question?")

    assert narrative == "Answer here."
    assert coverage_exceeded is True


async def test_answer_chat_turn_calls_call_groq_exactly_once_with_max_tokens() -> None:
    body = _full_memo_body()
    mock_call = AsyncMock(
        return_value=_groq_result('Answer.\n```json\n{"coverage_exceeded": false}\n```')
    )
    with patch("app.services.chat_service.call_groq", mock_call):
        await answer_chat_turn(body, [], "A question?")

    mock_call.assert_awaited_once()
    _, kwargs = mock_call.await_args
    assert kwargs["max_tokens"] == 1024


async def test_answer_chat_turn_prompt_contains_all_required_parts() -> None:
    body = _full_memo_body()
    history = [_FakeMessage("user", "Earlier question text")]
    mock_call = AsyncMock(
        return_value=_groq_result('Answer.\n```json\n{"coverage_exceeded": false}\n```')
    )
    with patch("app.services.chat_service.call_groq", mock_call):
        await answer_chat_turn(body, history, "New question text")

    prompt = mock_call.await_args.args[0]
    assert "DATA" in prompt
    assert "Revenue grew 12% YoY." in prompt
    assert "Earlier question text" in prompt
    assert "New question text" in prompt
    assert "coverage_exceeded" in prompt


async def test_answer_chat_turn_no_fence_returns_narrative_and_false() -> None:
    body = _full_memo_body()
    with patch(
        "app.services.chat_service.call_groq",
        AsyncMock(return_value=_groq_result("A narrative answer with no fence at all.")),
    ):
        narrative, coverage_exceeded = await answer_chat_turn(body, [], "A question?")

    assert narrative == "A narrative answer with no fence at all."
    assert coverage_exceeded is False


# ---------------------------------------------------------------------------
# Import-boundary source review (D-12 / T-08-BOUNDARY-GROQ)
# ---------------------------------------------------------------------------


def test_chat_service_uses_call_groq_and_never_imports_groq_sdk_directly() -> None:
    """chat_service.py imports call_groq from app.services.groq_client and
    never imports the groq SDK's client class or package directly (D-12).
    The repo's CI import-guard (tests/test_boundaries.py) only walks
    app.agents/ and app.graph/, not app.services/, so this service-local
    test IS the enforcement for chat_service.py (T-08-BOUNDARY-GROQ). Only
    actual import statement lines are checked (not docstrings/comments,
    which may legitimately reference the forbidden names when documenting
    the rule)."""
    assert chat_service.call_groq is not None
    import_lines = [
        line.strip()
        for line in inspect.getsource(chat_service).splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    # The only permitted groq-related import is `from app.services.groq_client
    # import call_groq` — a bare `import groq` or `from groq import ...` (the
    # SDK package itself) is a boundary violation.
    forbidden = [
        line
        for line in import_lines
        if (line.startswith("import groq") or line.startswith("from groq "))
    ]
    assert forbidden == [], f"Forbidden direct groq SDK import(s): {forbidden}"
