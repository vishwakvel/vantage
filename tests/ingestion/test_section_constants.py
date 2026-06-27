"""Unit tests for app.ingestion.section_constants.

Verifies:
- All module-level public constants are non-empty strings (no accidental empty values)
- Required named constants exist and carry correct string values
- Adding a new constant doesn't silently break the non-empty invariant
"""

import inspect

from app.ingestion import section_constants


def test_all_public_constants_are_non_empty_strings() -> None:
    """Every public module-level name in section_constants is a non-empty str.

    Iterates all names not starting with '_', filtering out submodules and
    functions.  Any empty string or non-string value causes the test to fail,
    ensuring future additions don't silently introduce blank constants.
    """
    public_members = {
        k: v
        for k, v in vars(section_constants).items()
        if not k.startswith("_")
        and not inspect.ismodule(v)
        and not inspect.isfunction(v)
        and not inspect.isclass(v)
    }
    bad = {k: v for k, v in public_members.items() if not isinstance(v, str) or len(v) == 0}
    assert not bad, f"Non-string or empty constants found in section_constants: {bad}"


def test_required_constants_exist() -> None:
    """Named constants required by the agent pipeline are importable and correct."""
    assert hasattr(section_constants, "SECTION_RISK_FACTORS")
    assert hasattr(section_constants, "SECTION_MDA")
    assert hasattr(section_constants, "SECTION_FUNDAMENTALS")
    assert hasattr(section_constants, "SECTION_SYNTHESIS")

    assert section_constants.SECTION_RISK_FACTORS == "risk_factors"
    assert section_constants.SECTION_MDA == "mda"
    assert section_constants.SECTION_FUNDAMENTALS == "fundamentals"
    assert section_constants.SECTION_SYNTHESIS == "synthesis"


def test_edgar_section_constants_have_correct_values() -> None:
    """SEC EDGAR section identifiers carry correct string values."""
    assert section_constants.SECTION_BUSINESS == "business"
    assert section_constants.SECTION_FINANCIALS == "financials"
    assert section_constants.SECTION_NOTES == "notes"
    assert section_constants.SECTION_COVER == "cover"


def test_memo_section_constants_have_correct_values() -> None:
    """Research memo output section identifiers carry correct string values."""
    assert section_constants.SECTION_CONTRADICTIONS == "contradictions"
    assert section_constants.SECTION_RISKS == "risks"
    assert section_constants.SECTION_MACRO == "macro"
    assert section_constants.SECTION_COMPARABLES == "comparables"
    assert section_constants.SECTION_SENTIMENT == "sentiment"
