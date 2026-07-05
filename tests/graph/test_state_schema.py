"""Introspection tests for ``app.graph.state.AgentGraphState``.

Pure typing/annotation checks — no DB, no fixtures, no async I/O.
"""

from __future__ import annotations

from typing import get_type_hints

from app.graph.state import AgentGraphState

_RESOLVED_ANNOTATIONS = get_type_hints(AgentGraphState)

_PRE_EXISTING_KEYS = {
    "plan_id",
    "ticker",
    "user_id",
    "session",
    "fundamentals_output",
    "fundamentals_status",
    "synthesis_output",
    "synthesis_status",
    "memo_status",
}

_NEW_SPECIALIST_KEYS = {
    "sentiment_output",
    "sentiment_status",
    "risk_output",
    "risk_status",
    "macro_output",
    "macro_status",
    "comparables_output",
    "comparables_status",
}

_OUTPUT_KEYS = {
    key for key in _NEW_SPECIALIST_KEYS if key.endswith("_output")
}
_STATUS_KEYS = {
    key for key in _NEW_SPECIALIST_KEYS if key.endswith("_status")
}


def test_agent_graph_state_has_all_expected_keys() -> None:
    """AgentGraphState carries the 7 pre-existing keys plus the 8 new
    per-specialist output+status fields (15 total)."""
    annotations = set(AgentGraphState.__annotations__)
    expected = _PRE_EXISTING_KEYS | _NEW_SPECIALIST_KEYS

    assert annotations >= expected
    assert len(expected) == 17  # 9 pre-existing + 8 new


def test_pre_existing_keys_still_present() -> None:
    """Original fields (fundamentals_*, synthesis_*, memo_status, session,
    plan_id, ticker, user_id) are untouched."""
    annotations = set(AgentGraphState.__annotations__)
    assert _PRE_EXISTING_KEYS <= annotations


def test_new_output_fields_permit_none() -> None:
    """Each new *_output key annotation permits None (dict | None)."""
    for key in _OUTPUT_KEYS:
        annotation = _RESOLVED_ANNOTATIONS[key]
        assert type(None) in getattr(annotation, "__args__", ()), (
            f"{key} annotation {annotation!r} must permit None"
        )


def test_new_status_fields_are_str() -> None:
    """Each new *_status key is typed str."""
    for key in _STATUS_KEYS:
        annotation = _RESOLVED_ANNOTATIONS[key]
        assert annotation is str, f"{key} annotation {annotation!r} must be 'str'"
