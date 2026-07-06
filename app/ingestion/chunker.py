"""Section-aware chunker for SEC EDGAR filing HTML.

Converts raw filing HTML into model-sized, section-tagged chunks.

Design constraints (from 02-RESEARCH.md):
  - No BeautifulSoup or lxml: HTML is stripped via re (no extra deps)
  - Chunk size: <=250 whitespace-delimited words (all-MiniLM-L6-v2 limit)
  - Overlap: ~50 words between consecutive sub-chunks of the same section
  - Section values: imported from section_constants ONLY — no inline literals

Heading detection (regression fix): a bare "Item N" match ANYWHERE in the
flattened filing text used to be treated as a new section heading —
including an inline cross-reference embedded in running prose, e.g. "...see
Item 1A of our most recent Annual Report...". That silently reclassified
unrelated MD&A content (gross margin, tax rate, dividends) as risk-factors
disclosure. A match now only counts as a genuine heading when BOTH hold:
  1. Position — it starts its own line. Real EDGAR headings are always
     their own block-level element (<p>/<div>/<tr>/... ); a cross-reference
     is just a phrase inside a body paragraph. This is why HTML tags are
     stripped in two passes (_linearize_html): block-level tags become a
     newline so a genuine heading lands on its own line, everything else
     becomes a space, exactly as before.
  2. Title shape — what follows on that line reads like the start of a
     section title ("Item 1A. Risk Factors"), not a cross-reference
     continuing on to name some OTHER document/location ("Item 1A of our
     Annual Report...", "Item 1A herein"). See _looks_like_section_title.

Public API:
  ITEM_TO_SECTION  — ordered mapping from item-label regex to section constant
  word_split()     — split text into overlapping word windows
  section_aware_chunk() — full HTML → [{text, metadata}] pipeline
"""

from __future__ import annotations

import re
from html import unescape
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

# Block-level tags that create a visual line/paragraph break when an EDGAR
# filing is rendered. Normalized to a literal newline (never a bare space)
# so a genuine section heading -- always its own block element in a real
# filing -- lands on its own line, while an inline cross-reference like
# "...see Item 1A of our Annual Report..." stays embedded inside its
# surrounding paragraph's single line. This is the positional signal
# _is_heading_line() relies on.
_BLOCK_TAG_RE: re.Pattern[str] = re.compile(
    r"</?(?:p|div|br|tr|td|li|h[1-6]|hr)\b[^>]*/?>",
    re.IGNORECASE,
)

# Any other HTML tag (inline formatting like <strong>/<em>/<span>) --
# stripped to a single space, same as the original behaviour.
_HTML_TAG_RE: re.Pattern[str] = re.compile(r"<[^>]+>")

# A heading candidate: "Item N" or "Item NA" at the very start of a line
# (ignoring leading whitespace). Group 2 captures the rest of that line,
# validated by _looks_like_section_title() below.
_LINE_HEADING_RE: re.Pattern[str] = re.compile(
    r"^\s*(item\s+\d+[a-zA-Z]?)\b(.*)$",
    re.IGNORECASE,
)

# Leading separator punctuation/whitespace between an "Item N" token and
# its title ("Item 1A. Risk Factors" -> ". " is the separator).
_HEADING_SEPARATOR_RE: re.Pattern[str] = re.compile(r"^[\s.:;,\-–—]+")

_FIRST_WORD_RE: re.Pattern[str] = re.compile(r"[A-Za-z']+")

# Words that indicate an "Item N" match is a cross-reference embedded in
# running prose ("Item 1A of our most recent Annual Report...", "Item 1A
# herein") rather than the start of that section's own title ("Item 1A.
# Risk Factors"). A real heading's title is a noun phrase and never
# continues with one of these connective/referential words.
_CROSS_REFERENCE_WORDS: frozenset[str] = frozenset(
    {
        "of",
        "in",
        "under",
        "herein",
        "hereof",
        "hereto",
        "above",
        "below",
        "contained",
        "referenced",
        "described",
        "discussed",
        "incorporated",
        "set",
    }
)


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


def _linearize_html(html: str) -> str:
    """Convert HTML to plain text, preserving line breaks at block tags.

    Order matters:
      1. Collapse ALL pre-existing whitespace in the raw source (including
         any incidental newlines from HTML pretty-printing) to single
         spaces FIRST, so the only newlines that survive are the ones this
         function deliberately inserts in step 2 -- never an artifact of
         how the source HTML happened to be formatted.
      2. Replace block-level tags with a newline (see _BLOCK_TAG_RE).
      3. Strip any remaining (inline) tags to a single space -- same as
         the original behaviour.
      4. Tidy up: trim horizontal whitespace hugging each newline and
         collapse blank lines.
      5. Decode HTML entities LAST, only after all tag-stripping is done.
         Real EDGAR filings routinely separate a heading's item number
         from its title with "&#160;" (non-breaking space) rather than a
         literal space -- e.g. "Item 1A.&#160;&#160;&#160;&#160;Risk
         Factors" -- which _is_heading_line's whitespace-based checks
         would otherwise never recognize as a separator. Decoding must
         happen AFTER tag-stripping (not before): an entity like "&lt;"
         decodes to a literal "<", which -- if decoded while tag regexes
         still had text left to scan -- could be misread as opening a new
         tag and corrupt unrelated content after it.
    """
    text = re.sub(r"\s+", " ", html)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n+", "\n", text)
    return unescape(text.strip())


def _match_section(header: str) -> str | None:
    """Return the section constant for an EDGAR Item header string, or None."""
    for pattern, section in _COMPILED_PATTERNS:
        if pattern.search(header):
            return section
    return None


def _looks_like_section_title(tail: str) -> bool:
    """True if *tail* (the text on a heading candidate's own line, right
    after its "Item N" token) reads like the start of that section's own
    title ("Item 1A. Risk Factors") rather than a cross-reference sentence
    naming some OTHER document/location ("Item 1A of our most recent
    Annual Report...", "Item 1A herein"). Case-insensitive, matching
    _match_section's own case-insensitive matching.
    """
    remainder = _HEADING_SEPARATOR_RE.sub("", tail)
    first_word_match = _FIRST_WORD_RE.match(remainder)
    if first_word_match is None:
        return False
    return first_word_match.group(0).lower() not in _CROSS_REFERENCE_WORDS


def _is_heading_line(line: str) -> tuple[str, str] | None:
    """Return (header_token, tail) if *line* is a genuine section heading.

    A line only counts as a heading when BOTH hold:
      1. It starts with an "Item N"/"Item NA" token -- the positional
         signal (see module docstring).
      2. What follows on that same line looks like a section title, not a
         cross-reference continuation (see _looks_like_section_title).

    Returns None if either check fails.
    """
    match = _LINE_HEADING_RE.match(line)
    if match is None:
        return None
    header, tail = match.group(1), match.group(2)
    if not _looks_like_section_title(tail):
        return None
    return header, tail


# ---------------------------------------------------------------------------
# section_aware_chunk
# ---------------------------------------------------------------------------


def section_aware_chunk(
    html: str,
    base_metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Convert filing HTML into section-tagged, model-sized chunks.

    Processing steps:
      1. Linearize HTML to plain text, preserving line breaks at
         block-level tag boundaries (regex only — no external deps).
      2. Walk the text line by line. A line starts a new section only when
         it is a genuine heading — see _is_heading_line() and the module
         docstring for why a bare "Item N" match is not enough (it must
         also be followed by title-shaped text, not a cross-reference).
         Text before the first heading is tagged SECTION_COVER.
      3. For each section's accumulated body, call word_split() to produce
         <=250-word windows with 50-word overlap.
      4. Merge base_metadata with section and chunk_index into each chunk.

    Args:
        html:          Raw HTML of an EDGAR filing document.
        base_metadata: Metadata dict with at least: canonical_id, ticker,
                       form_type, period_of_report, user_id.

    Returns:
        List of dicts: [{"text": str, "metadata": dict}, ...]
        chunk_index is a monotonically incrementing int starting at 0.
        Returns [] for empty / tag-only input.
    """
    linear = _linearize_html(html)
    if not linear:
        return []

    # Collect (section_constant, section_body_text) pairs. A section whose
    # heading doesn't map to a known Item (e.g. "Item 2") is intentionally
    # dropped, matching the original behaviour for unmapped items.
    sections: list[tuple[str, str]] = []

    current_section: str | None = SECTION_COVER  # text before first heading
    current_lines: list[str] = []

    def _flush_current_section() -> None:
        if current_section is None:
            return  # dropped segment (unmapped item heading)
        body = " ".join(current_lines).strip()
        if body:
            sections.append((current_section, body))

    for line in linear.split("\n"):
        stripped_line = line.strip()
        if not stripped_line:
            continue
        heading = _is_heading_line(stripped_line)
        if heading is not None:
            header, tail = heading
            _flush_current_section()
            current_section = _match_section(header)
            current_lines = [tail] if tail.strip() else []
        else:
            current_lines.append(stripped_line)

    _flush_current_section()

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
