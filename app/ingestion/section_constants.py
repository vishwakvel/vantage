"""Section name constants — single source of truth.

All section string literals used across ingestion, agents, and retrieval MUST
be imported from this module.  Never define section name strings as inline
literals elsewhere in the codebase.

The ingestion pipeline (Phase 2+), retrieval layer, and agent outputs all
reference these constants via::

    from app.ingestion.section_constants import SECTION_RISK_FACTORS, SECTION_MDA

Adding a new section: add the constant here ONLY, then import it where needed.
"""

# ---------------------------------------------------------------------------
# SEC EDGAR filing sections  (10-K / 10-Q section identifiers)
# ---------------------------------------------------------------------------

SECTION_RISK_FACTORS: str = "risk_factors"
SECTION_MDA: str = "mda"
SECTION_BUSINESS: str = "business"
SECTION_FINANCIALS: str = "financials"
SECTION_NOTES: str = "notes"
SECTION_COVER: str = "cover"

# ---------------------------------------------------------------------------
# News and research article sections
# ---------------------------------------------------------------------------

SECTION_HEADLINE: str = "headline"
SECTION_BODY: str = "body"
SECTION_ABSTRACT: str = "abstract"

# ---------------------------------------------------------------------------
# Research memo output sections  (used by agents in Phase 4+)
# ---------------------------------------------------------------------------

SECTION_FUNDAMENTALS: str = "fundamentals"
SECTION_SYNTHESIS: str = "synthesis"
SECTION_CONTRADICTIONS: str = "contradictions"
SECTION_RISKS: str = "risks"
SECTION_MACRO: str = "macro"
SECTION_COMPARABLES: str = "comparables"
SECTION_SENTIMENT: str = "sentiment"
