"""Chat service — linear, single-call chat brain for a memo's follow-up chat.

Assembles grounding context (full memo body per D-10 + last-8-message
history window per D-11) into ONE ``call_groq()`` completion, then splits
the narrative answer from a trailing fenced JSON coverage flag. No
LangGraph node, no agent loop, no second call — this is a pure
request/response function invoked once per ``POST
/research/memo/{memo_id}/chat`` request (08-02-PLAN.md, mirrors
``app/agents/synthesis.py``'s call -> split -> parse pipeline, collapsed
to a single turn with no AgentTask/AgentOutput rows — the route (08-03)
persists ChatMessage rows instead).

This module NEVER imports ``groq``/``AsyncGroq`` directly (D-12) — only
``call_groq`` from ``app.services.groq_client``, the sole rate-limited path
to the Groq API (imported by Task 2's ``answer_chat_turn``).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import json_repair
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from app.db.models import ChatMessage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

#: D-11 — only the last 8 history messages are resent as prior conversation
#: (sliding window, no summarization, no full resend).
_HISTORY_WINDOW: int = 8

#: Matches a fenced ```json ... ``` block anywhere in the model's raw text
#: response (DOTALL so the fenced content can span multiple lines). Copied
#: verbatim from app/agents/synthesis.py.
_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)

#: Fixed, ordered list of specialist section keys serialized into the
#: grounding prompt (D-10 — full memo body, never trimmed to synthesis-only).
_SPECIALIST_SECTIONS: tuple[str, ...] = (
    "fundamentals",
    "sentiment",
    "risks",
    "macro",
    "comparables",
)


class CoverageFlag(BaseModel):
    """Validated shape of the fenced coverage-JSON block (D-08/D-09).

    Mirrors ``app.agents.synthesis.ContradictionItem``'s role: a narrow,
    single-purpose validation boundary around one self-reported LLM field.
    """

    coverage_exceeded: bool


# ---------------------------------------------------------------------------
# Parse / split helpers (never raise — fail open toward the narrative)
# ---------------------------------------------------------------------------


def _split_narrative_and_json(raw_text: str) -> tuple[str, str | None]:
    """Split the model's raw completion into (narrative, fenced_json_str).

    Never raises. Returns the text before the first ```json fence as the
    narrative, and the fenced content as the second element. When no fence
    is found, the entire stripped text is returned as the narrative and the
    second element is ``None`` — the narrative always stays usable even if
    the coverage-flag portion is absent or malformed. Copied near-verbatim
    from ``app.agents.synthesis._split_narrative_and_json``.
    """
    try:
        match = _FENCE_RE.search(raw_text)
    except TypeError:
        logger.warning("Chat raw output was not a string; no split performed")
        return "", None
    if match is None:
        return raw_text.strip(), None
    narrative = raw_text[: match.start()].strip()
    return narrative, match.group(1)


def _parse_coverage_flag(fenced_json_str: str | None) -> bool:
    """Parse, repair, and validate the fenced coverage-flag JSON payload.

    Never raises — defaults to ``False`` (i.e. assume covered, do NOT show
    the warning badge) on any unrecoverable failure. This default direction
    matters: a parse failure should never manufacture a false coverage
    warning the model never actually gave; it should just omit the badge
    and let the narrative answer stand on its own, same
    failure-open-to-narrative philosophy as
    ``synthesis.py::_parse_contradictions``.
    """
    if not fenced_json_str:
        return False
    try:
        raw = json.loads(fenced_json_str)
    except json.JSONDecodeError:
        try:
            raw = json_repair.loads(fenced_json_str)
        except Exception:  # noqa: BLE001 — never raise past this helper
            logger.warning("Coverage-flag JSON repair failed; defaulting to False")
            return False
    try:
        return CoverageFlag(**raw).coverage_exceeded
    except (ValidationError, TypeError):
        logger.warning("Coverage-flag payload failed validation; defaulting to False")
        return False


# ---------------------------------------------------------------------------
# Grounding-context serialization
# ---------------------------------------------------------------------------


def _serialize_memo(memo_body: dict[str, Any]) -> str:
    """D-10: serialize ALL five specialist sections + synthesis.take +
    contradictions — never trim to synthesis-only (would cause false
    ``coverage_exceeded`` flags on legitimate section-specific questions).

    Each specialist section is None-guarded independently (mirrors
    ``synthesis._build_prompt``'s per-source guard) — a missing/failed
    section emits an explicit "unavailable" line rather than raising or
    silently omitting the section.
    """
    blocks: list[str] = []
    for section in _SPECIALIST_SECTIONS:
        section_data = memo_body.get(section) or {}
        narrative = section_data.get("narrative")
        if narrative:
            blocks.append(f"{section.upper()}:\n{narrative}")
        else:
            blocks.append(f"{section.upper()}: unavailable for this memo.")

    synthesis = memo_body.get("synthesis") or {}
    take = synthesis.get("take")
    if take:
        blocks.append(f"SYNTHESIS TAKE:\n{take}")
    else:
        blocks.append("SYNTHESIS TAKE: unavailable for this memo.")

    contradictions = synthesis.get("contradictions") or []
    if contradictions:
        contradiction_lines = "\n".join(
            f"- [{item.get('severity', 'Unknown')}] {item.get('topic', '')}: "
            f"{item.get('description', '')}"
            for item in contradictions
        )
        blocks.append(f"CONTRADICTIONS:\n{contradiction_lines}")
    else:
        blocks.append("CONTRADICTIONS: none identified.")

    return "\n\n".join(blocks)


def _serialize_history(messages: list[ChatMessage]) -> str:
    """D-11: last 8 rows only (sliding window), oldest-first within that
    window, role-labeled (user/assistant) — no summarization, no full
    resend.
    """
    window = messages[-_HISTORY_WINDOW:]
    return "\n".join(f"{m.role}: {m.content}" for m in window)
