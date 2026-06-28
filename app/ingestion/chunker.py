"""Section-aware chunker for SEC EDGAR filing HTML.

Converts raw filing HTML into model-sized, section-tagged chunks.

Design constraints (from 02-RESEARCH.md):
  - No BeautifulSoup or lxml: HTML is stripped via re (no extra deps)
  - Chunk size: <=250 whitespace-delimited words (all-MiniLM-L6-v2 limit)
  - Overlap: ~50 words between consecutive sub-chunks of the same section
  - Section values: imported from section_constants ONLY — no inline literals

Public API:
  ITEM_TO_SECTION  — ordered mapping from item-label regex to section constant
  word_split()     — split text into overlapping word windows
  section_aware_chunk() — full HTML → [{text, metadata}] pipeline
"""

from __future__ import annotations

import re
from typing import Any

from app.ingestion.section_constants import (
    SECTION_BUSINESS,
    SECTION_COVER,
    SECTION_FINANCIALS,
    SECTION_MDA,
    SECTION_NOTES,
    SECTION_RISK_FACTORS,
)

# ---------------------------------------------------------------------------
# ITEM_TO_SECTION — ordered mapping: regex key → section constant
#
# Order matters: more-specific patterns (e.g. "item 1a") MUST come before
# their prefixes (e.g. "item 1") so the first match wins.
# Values are section_constants imports — no inline section string literals.
# ---------------------------------------------------------------------------

ITEM_TO_SECTION: dict[str, str] = {
    r"item\s+1a\b": SECTION_RISK_FACTORS,   # Item 1A before Item 1
    r"item\s+1\b": SECTION_BUSINESS,        # Item 1 (not 1A)
    r"item\s+7a\b": SECTION_FINANCIALS,     # Item 7A before Item 7
    r"item\s+7\b": SECTION_MDA,             # Item 7 (not 7A)
    r"item\s+8\b": SECTION_FINANCIALS,      # Item 8
    r"item\s+15\b": SECTION_NOTES,          # Item 15
}

# Pre-compiled pattern list for O(1) ordered lookup at chunk time
_COMPILED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pattern, re.IGNORECASE), section)
    for pattern, section in ITEM_TO_SECTION.items()
]

# Split regex: any "Item N" or "Item NA" heading (case-insensitive)
_ITEM_SPLIT_RE: re.Pattern[str] = re.compile(
    r"(\bitem\s+\d+[a-zA-Z]?\b)",
    re.IGNORECASE,
)

# HTML tag strip regex
_HTML_TAG_RE: re.Pattern[str] = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# word_split
# ---------------------------------------------------------------------------


def word_split(
    text: str,
    max_words: int = 250,
    overlap: int = 50,
) -> list[str]:
    """Split *text* into overlapping word windows of at most *max_words* words.

    Args:
        text:      Whitespace-delimited plain text to split.
        max_words: Maximum words per window (inclusive). Default 250.
        overlap:   Number of words shared between consecutive windows. Default 50.

    Returns:
        Ordered list of text windows.  Empty list if *text* has no words.
    """
    words = text.split()
    if not words:
        return []

    step = max_words - overlap  # advance by (max_words - overlap) words each iteration
    if step <= 0:
        step = max_words  # degenerate case: no advance possible without overlap > limit

    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = start + max_words
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        start += step

    return chunks


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    """Remove HTML tags and normalise whitespace to single spaces."""
    text = _HTML_TAG_RE.sub(" ", html)
    return re.sub(r"\s+", " ", text).strip()


def _match_section(header: str) -> str | None:
    """Return the section constant for an EDGAR Item header string, or None."""
    for pattern, section in _COMPILED_PATTERNS:
        if pattern.search(header):
            return section
    return None


# ---------------------------------------------------------------------------
# section_aware_chunk
# ---------------------------------------------------------------------------


def section_aware_chunk(
    html: str,
    base_metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Convert filing HTML into section-tagged, model-sized chunks.

    Processing steps:
      1. Strip all HTML tags (regex only — no external deps).
      2. Split plain text on EDGAR Item headings, keeping delimiters.
      3. Map each heading to a section_constants value via ITEM_TO_SECTION.
         Text before the first Item heading is tagged SECTION_COVER.
      4. For each section block, call word_split() to produce <=250-word
         windows with 50-word overlap.
      5. Merge base_metadata with section and chunk_index into each chunk.

    Args:
        html:          Raw HTML of an EDGAR filing document.
        base_metadata: Metadata dict with at least: canonical_id, ticker,
                       form_type, period_of_report, user_id.

    Returns:
        List of dicts: [{"text": str, "metadata": dict}, ...]
        chunk_index is a monotonically incrementing int starting at 0.
        Returns [] for empty / tag-only input.
    """
    plain = _strip_html(html)
    if not plain:
        return []

    # Split on Item headings, retaining the delimiter (captured group)
    # parts = [pre_item, header1, body1, header2, body2, ...]
    parts = _ITEM_SPLIT_RE.split(plain)

    # Collect (section_constant, section_body_text) pairs
    sections: list[tuple[str, str]] = []

    # Text before the first Item heading → SECTION_COVER
    cover_text = parts[0].strip()
    if cover_text:
        sections.append((SECTION_COVER, cover_text))

    # Item sections: parts come in (header, body) pairs starting at index 1
    for i in range(1, len(parts), 2):
        header = parts[i]
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        section = _match_section(header)
        # Skip unknown items (e.g. Item 2, Item 3 — not in ITEM_TO_SECTION)
        if section is not None and body:
            sections.append((section, body))

    # Build output chunks
    chunks: list[dict[str, Any]] = []
    chunk_index = 0

    for section_name, section_text in sections:
        sub_chunks = word_split(section_text, max_words=250, overlap=50)
        for chunk_text in sub_chunks:
            if chunk_text.strip():
                metadata = {
                    **base_metadata,
                    "section": section_name,
                    "chunk_index": chunk_index,
                }
                chunks.append({"text": chunk_text, "metadata": metadata})
                chunk_index += 1

    return chunks
