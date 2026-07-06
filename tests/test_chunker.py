"""Unit tests for app/ingestion/chunker.py.

Tests verify:
  - word_split() produces windows of <=250 words with ~50-word overlap
  - section_aware_chunk() maps EDGAR Item headers to section_constants values
  - Chunk metadata contains section (from constants) and chunk_index (int)
  - No chunk exceeds 250 whitespace-delimited words
  - Overlap is present between consecutive same-section sub-chunks
  - item 1A maps to SECTION_RISK_FACTORS; item 7 maps to SECTION_MDA
  - cover text (before first Item) maps to SECTION_COVER
  - base_metadata fields are merged into every chunk's metadata
  - ITEM_TO_SECTION values are the section_constants (no inline literals)
"""

from __future__ import annotations

import re

import pytest

from app.ingestion.chunker import ITEM_TO_SECTION, section_aware_chunk, word_split
from app.ingestion.section_constants import (
    SECTION_BUSINESS,
    SECTION_COVER,
    SECTION_FINANCIALS,
    SECTION_MDA,
    SECTION_NOTES,
    SECTION_RISK_FACTORS,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

BASE_META = {
    "canonical_id": "abc123",
    "ticker": "AAPL",
    "form_type": "10-K",
    "period_of_report": "2023-09-30",
    "user_id": "",
}


def _make_words(n: int, word: str = "word") -> str:
    """Return a string of exactly n whitespace-separated words."""
    return " ".join([word] * n)


# ---------------------------------------------------------------------------
# word_split — basic contract
# ---------------------------------------------------------------------------


def test_word_split_single_chunk_when_under_limit() -> None:
    """word_split returns one chunk if text has <=max_words words."""
    text = _make_words(100)
    result = word_split(text, max_words=250, overlap=50)
    assert len(result) == 1
    assert len(result[0].split()) == 100


def test_word_split_exact_limit_is_one_chunk() -> None:
    """word_split returns one chunk for exactly max_words words."""
    text = _make_words(250)
    result = word_split(text, max_words=250, overlap=50)
    assert len(result) == 1
    assert len(result[0].split()) == 250


def test_word_split_produces_multiple_chunks_over_limit() -> None:
    """word_split produces multiple chunks when text exceeds max_words."""
    text = _make_words(400)  # 400 words > 250 limit
    result = word_split(text, max_words=250, overlap=50)
    assert len(result) >= 2


def test_word_split_no_chunk_exceeds_max_words() -> None:
    """Every chunk from word_split has <=max_words words."""
    text = _make_words(600)
    result = word_split(text, max_words=250, overlap=50)
    for chunk in result:
        assert len(chunk.split()) <= 250, f"Chunk has {len(chunk.split())} words"


def test_word_split_overlap_present() -> None:
    """Consecutive chunks from word_split share ~50 words of overlap."""
    # Use distinct words so we can detect overlap by content
    words = [f"w{i}" for i in range(400)]
    text = " ".join(words)
    result = word_split(text, max_words=250, overlap=50)

    assert len(result) >= 2
    chunk0_words = result[0].split()
    chunk1_words = result[1].split()

    # The last `overlap` words of chunk0 should appear at the start of chunk1
    overlap_from_0 = chunk0_words[-50:]
    start_of_1 = chunk1_words[:50]
    assert overlap_from_0 == start_of_1, (
        f"Expected 50-word overlap but got: chunk0 tail={overlap_from_0[:5]}…, "
        f"chunk1 head={start_of_1[:5]}…"
    )


def test_word_split_empty_text_returns_empty_list() -> None:
    """word_split on empty string returns []."""
    assert word_split("", max_words=250, overlap=50) == []


def test_word_split_whitespace_only_returns_empty() -> None:
    """word_split on whitespace-only string returns []."""
    assert word_split("   \t\n  ", max_words=250, overlap=50) == []


# ---------------------------------------------------------------------------
# section_aware_chunk — section mapping
# ---------------------------------------------------------------------------


MINIMAL_10K_HTML = """
<html><body>
<p>Cover page text about the company filing.</p>
<p>ITEM 1A. Risk Factors</p>
<p>The company faces many risks including market risk and operational risk.
These risks are described in detail below and may materially affect the business.</p>
<p>ITEM 7. Management Discussion and Analysis</p>
<p>Revenue increased 10 percent year over year. Operating income was strong.
Management is optimistic about future quarters and guidance for FY2024.</p>
<p>ITEM 8. Financial Statements</p>
<p>See consolidated financial statements attached hereto.</p>
</body></html>
"""


def test_chunks_contain_section_risk_factors_for_item_1a() -> None:
    """Item 1A heading maps chunk metadata.section to SECTION_RISK_FACTORS."""
    chunks = section_aware_chunk(MINIMAL_10K_HTML, BASE_META)
    sections = [c["metadata"]["section"] for c in chunks]
    assert SECTION_RISK_FACTORS in sections, (
        f"Expected {SECTION_RISK_FACTORS!r} but got sections: {set(sections)}"
    )


def test_chunks_contain_section_mda_for_item_7() -> None:
    """Item 7 (not 7A) heading maps chunk metadata.section to SECTION_MDA."""
    chunks = section_aware_chunk(MINIMAL_10K_HTML, BASE_META)
    sections = [c["metadata"]["section"] for c in chunks]
    assert SECTION_MDA in sections, (
        f"Expected {SECTION_MDA!r} but got sections: {set(sections)}"
    )


def test_chunks_contain_section_financials_for_item_8() -> None:
    """Item 8 heading maps chunk metadata.section to SECTION_FINANCIALS."""
    chunks = section_aware_chunk(MINIMAL_10K_HTML, BASE_META)
    sections = [c["metadata"]["section"] for c in chunks]
    assert SECTION_FINANCIALS in sections, (
        f"Expected {SECTION_FINANCIALS!r} but got sections: {set(sections)}"
    )


def test_cover_text_maps_to_section_cover() -> None:
    """Text before the first Item heading maps to SECTION_COVER."""
    html = "<p>Annual Report cover page summary text.</p><p>Item 1. Business</p><p>We sell things.</p>"
    chunks = section_aware_chunk(html, BASE_META)
    cover_chunks = [c for c in chunks if c["metadata"]["section"] == SECTION_COVER]
    assert cover_chunks, "Expected at least one chunk with section=SECTION_COVER"


def test_no_section_cover_when_no_cover_text() -> None:
    """SECTION_COVER is absent when HTML starts directly with an Item heading."""
    html = "<p>Item 1A. Risk Factors</p><p>" + _make_words(20) + "</p>"
    chunks = section_aware_chunk(html, BASE_META)
    cover_chunks = [c for c in chunks if c["metadata"]["section"] == SECTION_COVER]
    assert len(cover_chunks) == 0, "Expected no cover chunks when no pre-item text"


def test_item_1_not_1a_maps_to_section_business() -> None:
    """Item 1 (not Item 1A) maps to SECTION_BUSINESS, not SECTION_RISK_FACTORS."""
    html = "<p>Item 1. Business</p><p>" + _make_words(30) + "</p>"
    chunks = section_aware_chunk(html, BASE_META)
    business_chunks = [c for c in chunks if c["metadata"]["section"] == SECTION_BUSINESS]
    risk_chunks = [c for c in chunks if c["metadata"]["section"] == SECTION_RISK_FACTORS]
    assert business_chunks, "Item 1 should map to SECTION_BUSINESS"
    assert not risk_chunks, "Item 1 should NOT map to SECTION_RISK_FACTORS"


def test_item_1a_not_confused_with_item_1() -> None:
    """Item 1A maps to SECTION_RISK_FACTORS, distinct from Item 1 → SECTION_BUSINESS."""
    html = (
        "<p>Item 1. Business</p><p>" + _make_words(20) + "</p>"
        "<p>Item 1A. Risk Factors</p><p>" + _make_words(20) + "</p>"
    )
    chunks = section_aware_chunk(html, BASE_META)
    sections = {c["metadata"]["section"] for c in chunks}
    assert SECTION_BUSINESS in sections
    assert SECTION_RISK_FACTORS in sections


def test_item_15_maps_to_section_notes() -> None:
    """Item 15 heading maps to SECTION_NOTES."""
    html = "<p>Item 15. Exhibits and Financial Statement Schedules</p><p>" + _make_words(20) + "</p>"
    chunks = section_aware_chunk(html, BASE_META)
    notes_chunks = [c for c in chunks if c["metadata"]["section"] == SECTION_NOTES]
    assert notes_chunks, "Expected at least one chunk with section=SECTION_NOTES"


# ---------------------------------------------------------------------------
# section_aware_chunk — chunk size invariant
# ---------------------------------------------------------------------------


def test_no_chunk_exceeds_250_words() -> None:
    """Every chunk produced by section_aware_chunk has <=250 words."""
    # Create a large filing with a 500-word Risk Factors section
    long_section = _make_words(500)
    html = f"<p>Item 1A. Risk Factors</p><p>{long_section}</p>"
    chunks = section_aware_chunk(html, BASE_META)
    for chunk in chunks:
        word_count = len(chunk["text"].split())
        assert word_count <= 250, f"Chunk has {word_count} words (limit 250)"


def test_no_chunk_exceeds_250_words_multiple_sections() -> None:
    """250-word limit holds across all sections in a realistic filing."""
    big_mda = _make_words(600)
    big_risks = _make_words(450)
    html = (
        f"<p>Item 1A. Risk Factors</p><p>{big_risks}</p>"
        f"<p>Item 7. Management Discussion</p><p>{big_mda}</p>"
    )
    chunks = section_aware_chunk(html, BASE_META)
    assert chunks, "Expected at least one chunk"
    max_words = max(len(c["text"].split()) for c in chunks)
    assert max_words <= 250, f"Largest chunk has {max_words} words"


# ---------------------------------------------------------------------------
# section_aware_chunk — overlap between same-section sub-chunks
# ---------------------------------------------------------------------------


def test_overlap_between_consecutive_same_section_chunks() -> None:
    """Consecutive sub-chunks of the same section share ~50 words of overlap."""
    # Build a 400-word Risk Factors section to force 2 sub-chunks
    words = [f"rf{i}" for i in range(400)]
    long_risk = " ".join(words)
    html = f"<p>Item 1A. Risk Factors</p><p>{long_risk}</p>"
    chunks = section_aware_chunk(html, BASE_META)

    risk_chunks = [c for c in chunks if c["metadata"]["section"] == SECTION_RISK_FACTORS]
    assert len(risk_chunks) >= 2, "Expected at least 2 sub-chunks for 400-word section"

    c0_words = risk_chunks[0]["text"].split()
    c1_words = risk_chunks[1]["text"].split()

    # Last 50 words of chunk 0 should be the first 50 words of chunk 1
    tail = c0_words[-50:]
    head = c1_words[:50]
    assert tail == head, f"Expected 50-word overlap. tail={tail[:3]}…, head={head[:3]}…"


# ---------------------------------------------------------------------------
# section_aware_chunk — chunk_index and metadata
# ---------------------------------------------------------------------------


def test_chunk_index_is_integer() -> None:
    """chunk_index in metadata is an int, not a string."""
    chunks = section_aware_chunk(MINIMAL_10K_HTML, BASE_META)
    for chunk in chunks:
        assert isinstance(chunk["metadata"]["chunk_index"], int), (
            f"chunk_index type is {type(chunk['metadata']['chunk_index'])}, expected int"
        )


def test_chunk_index_increments_monotonically() -> None:
    """chunk_index values form a consecutive 0-based sequence."""
    chunks = section_aware_chunk(MINIMAL_10K_HTML, BASE_META)
    indices = [c["metadata"]["chunk_index"] for c in chunks]
    assert indices == list(range(len(indices))), f"chunk_index sequence: {indices}"


def test_base_metadata_merged_into_every_chunk() -> None:
    """All base_metadata fields are present in every chunk's metadata."""
    chunks = section_aware_chunk(MINIMAL_10K_HTML, BASE_META)
    for chunk in chunks:
        for key, val in BASE_META.items():
            assert key in chunk["metadata"], f"Missing key {key!r} in chunk metadata"
            assert chunk["metadata"][key] == val, (
                f"Metadata key {key!r}: expected {val!r}, got {chunk['metadata'][key]!r}"
            )


def test_chunk_has_text_and_metadata_keys() -> None:
    """Every chunk dict has exactly 'text' and 'metadata' keys."""
    chunks = section_aware_chunk(MINIMAL_10K_HTML, BASE_META)
    for chunk in chunks:
        assert set(chunk.keys()) == {"text", "metadata"}, (
            f"Unexpected chunk keys: {set(chunk.keys())}"
        )


def test_chunk_text_is_non_empty_string() -> None:
    """No chunk has an empty or whitespace-only text value."""
    chunks = section_aware_chunk(MINIMAL_10K_HTML, BASE_META)
    assert chunks, "Expected at least one chunk"
    for chunk in chunks:
        assert isinstance(chunk["text"], str)
        assert chunk["text"].strip(), "Chunk text must not be empty or whitespace-only"


# ---------------------------------------------------------------------------
# ITEM_TO_SECTION mapping — constants-only contract
# ---------------------------------------------------------------------------


def test_item_to_section_values_are_section_constant_values() -> None:
    """Every value in ITEM_TO_SECTION is a known section_constants value."""
    known = {
        SECTION_RISK_FACTORS,
        SECTION_MDA,
        SECTION_BUSINESS,
        SECTION_FINANCIALS,
        SECTION_NOTES,
        SECTION_COVER,
    }
    for pattern, section in ITEM_TO_SECTION.items():
        assert section in known, (
            f"ITEM_TO_SECTION value {section!r} for pattern {pattern!r} "
            f"is not a known section_constants value"
        )


def test_item_to_section_is_non_empty() -> None:
    """ITEM_TO_SECTION contains at least 5 mappings."""
    assert len(ITEM_TO_SECTION) >= 5, (
        f"Expected >=5 item mappings, got {len(ITEM_TO_SECTION)}"
    )


# ---------------------------------------------------------------------------
# HTML stripping — chunker uses re, not BeautifulSoup
# ---------------------------------------------------------------------------


def test_html_tags_stripped_from_chunks() -> None:
    """Chunk text does not contain raw HTML tags."""
    html = "<p><strong>Item 1A.</strong> <em>Risk Factors</em></p><p>Risky text here.</p>"
    chunks = section_aware_chunk(html, BASE_META)
    for chunk in chunks:
        # No angle brackets should remain in chunk text
        assert "<" not in chunk["text"] and ">" not in chunk["text"], (
            f"HTML tag found in chunk text: {chunk['text']!r}"
        )


def test_section_aware_chunk_empty_html_returns_empty_list() -> None:
    """section_aware_chunk on empty HTML returns []."""
    chunks = section_aware_chunk("", BASE_META)
    assert chunks == []


def test_section_aware_chunk_no_items_html_returns_cover_chunk_or_empty() -> None:
    """HTML with no Item headers returns only cover chunk (or empty)."""
    html = "<p>Some company overview text without any items.</p>"
    chunks = section_aware_chunk(html, BASE_META)
    # Either empty (no text) or all chunks are SECTION_COVER
    for chunk in chunks:
        assert chunk["metadata"]["section"] == SECTION_COVER, (
            f"Without Item headers, expected SECTION_COVER but got "
            f"{chunk['metadata']['section']!r}"
        )


# ---------------------------------------------------------------------------
# Case-insensitive item detection
# ---------------------------------------------------------------------------


def test_item_header_case_insensitive() -> None:
    """Item headers are detected regardless of capitalisation."""
    html = "<p>item 1a. risk factors</p><p>" + _make_words(20) + "</p>"
    chunks_lower = section_aware_chunk(html, BASE_META)

    html_upper = "<p>ITEM 1A. RISK FACTORS</p><p>" + _make_words(20) + "</p>"
    chunks_upper = section_aware_chunk(html_upper, BASE_META)

    sections_lower = {c["metadata"]["section"] for c in chunks_lower}
    sections_upper = {c["metadata"]["section"] for c in chunks_upper}

    assert SECTION_RISK_FACTORS in sections_lower, "Lowercase 'item 1a' not detected"
    assert SECTION_RISK_FACTORS in sections_upper, "Uppercase 'ITEM 1A' not detected"


# ---------------------------------------------------------------------------
# Cross-reference regression (mid-sentence "Item 1A" is not a heading)
# ---------------------------------------------------------------------------


def test_cross_reference_to_item_1a_inside_mda_stays_mda() -> None:
    """A mid-paragraph cross-reference like "...see Item 1A of our Annual
    Report..." must NOT be treated as a Risk Factors heading -- it's a
    cross-reference embedded in running MD&A prose, not a section boundary.
    Real MD&A content appearing AFTER that phrase (tax rate, dividends)
    must stay tagged SECTION_MDA, not get split off into
    SECTION_RISK_FACTORS.

    Regression test for the bug where any bare "Item 1A" match anywhere in
    the flattened text -- including inside a sentence like "see Item 1A of
    our most recent Annual Report on Form 10-K" -- was treated as a new
    section heading, silently reclassifying unrelated MD&A content
    (gross margin, tax rate, dividends) as risk-factors disclosure.
    """
    html = (
        "<p>Item 7. Management Discussion and Analysis</p>"
        "<p>Our gross margin improved this quarter due to favorable product "
        "mix. For a discussion of factors that could affect our results, "
        "see Item 1A of our most recent Annual Report on Form 10-K. Our "
        "effective tax rate for the quarter was 15.5 percent. We declared "
        "a cash dividend of 0.24 dollars per share during the period, "
        "consistent with our capital return program.</p>"
    )
    chunks = section_aware_chunk(html, BASE_META)

    sections = {c["metadata"]["section"] for c in chunks}
    assert SECTION_RISK_FACTORS not in sections, (
        "Mid-sentence cross-reference to 'Item 1A' was wrongly treated as "
        "a Risk Factors heading"
    )
    assert SECTION_MDA in sections

    mda_text = " ".join(
        c["text"] for c in chunks if c["metadata"]["section"] == SECTION_MDA
    )
    assert "tax rate" in mda_text
    assert "dividend" in mda_text


def test_heading_at_start_of_own_line_still_detected_after_cross_reference() -> None:
    """A genuine heading is still detected even after a prior paragraph
    contains an inline "Item N" cross-reference -- the fix must not
    suppress real headings, only mid-sentence false positives."""
    html = (
        "<p>Item 7. Management Discussion and Analysis</p>"
        "<p>See Item 1A of our most recent Annual Report for risk "
        "discussion.</p>"
        "<p>Item 8. Financial Statements</p>"
        "<p>See consolidated financial statements attached hereto.</p>"
    )
    chunks = section_aware_chunk(html, BASE_META)
    sections = {c["metadata"]["section"] for c in chunks}
    assert SECTION_MDA in sections
    assert SECTION_FINANCIALS in sections
    assert SECTION_RISK_FACTORS not in sections


def test_heading_with_nbsp_entity_separator_is_detected() -> None:
    """Real EDGAR filings commonly separate a heading's item number from
    its title with "&#160;" (non-breaking space) entities instead of a
    literal space -- e.g. Apple's actual 10-Q markup renders the Risk
    Factors heading as:
        <span ...>Item 1A.&#160;&#160;&#160;&#160;Risk Factors</span>
    This must still be recognized as a genuine heading (regression test
    for a live-system finding: the position+title-shape check initially
    missed every real heading because "&#160;" isn't literal whitespace
    until HTML-entity-decoded).
    """
    html = (
        "<div><span>Item 1A.&#160;&#160;&#160;&#160;Risk Factors</span></div>"
        "<div><span>The Company's business, reputation, results of "
        "operations, and financial condition can be materially and "
        "adversely affected by a number of factors.</span></div>"
        "<div><span>Item 7.&#160;&#160;&#160;&#160;Management&#8217;s "
        "Discussion and Analysis</span></div>"
        "<div><span>Revenue increased and operating income was strong "
        "this quarter.</span></div>"
    )
    chunks = section_aware_chunk(html, BASE_META)
    sections = {c["metadata"]["section"] for c in chunks}
    assert SECTION_RISK_FACTORS in sections, (
        f"nbsp-separated 'Item 1A' heading not detected; got sections: {sections}"
    )
    assert SECTION_MDA in sections, (
        f"nbsp-separated 'Item 7' heading not detected; got sections: {sections}"
    )

    risk_text = " ".join(
        c["text"] for c in chunks if c["metadata"]["section"] == SECTION_RISK_FACTORS
    )
    assert "materially and adversely affected" in risk_text
