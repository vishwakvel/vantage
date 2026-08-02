"""Hand-curated RAGAS golden set + validating loader (D-07, OBS-03).

RAGAS's non-LLM retrieval metrics (``NonLLMContextPrecisionWithReference``,
``NonLLMContextRecall``) compare each retrieved chunk against the golden
set's ``reference_contexts`` entries using a normalized, full-string
Levenshtein similarity at a 0.5 threshold — not substring or keyword
containment. A short keyword phrase can never cross that threshold against a
multi-sentence retrieved chunk, so a keyword-shaped fixture would silently
produce near-zero scores for every case regardless of retrieval quality
(RESEARCH.md Pitfall 2). ``MIN_REFERENCE_CONTEXT_CHARS`` exists specifically
to make that mistake fail loudly at load time instead of shipping a
meaningless eval signal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Single source of truth for the shipped fixture's location.
GOLDEN_SET_PATH: Path = Path(__file__).with_name("ragas_golden_set.json")

# The non-LLM RAGAS metrics score a normalized full-string Levenshtein
# similarity between `retrieved_contexts` and `reference_contexts` at a 0.5
# threshold — a reference passage must be long enough to be length-comparable
# to a real multi-sentence retrieved chunk. A short phrase cannot cross that
# threshold against a real chunk no matter how good retrieval is (Pitfall 2).
MIN_REFERENCE_CONTEXT_CHARS: int = 200

# D-07's "~10-20 cases" band, as named constants rather than inline literals.
MIN_CASES: int = 10
MAX_CASES: int = 20

# The two allowed provenance values (see ragas_golden_set.json authoring
# notes): "verbatim-edgar" for passages copied from real filing text,
# "authored" for passages written in filing register without a verified
# source. This distinction lets plan 12-14's live smoke run interpret a low
# score correctly (an "authored" case scoring low is a fixture problem; a
# "verbatim-edgar" case scoring low is a retriever signal).
ALLOWED_PROVENANCE: frozenset[str] = frozenset({"verbatim-edgar", "authored"})

# The six required keys every case object must carry.
_REQUIRED_KEYS: tuple[str, ...] = (
    "case_id",
    "query",
    "ticker",
    "user_id",
    "provenance",
    "reference_contexts",
)

# Keys whose values must be non-empty strings (user_id may be empty — it is
# the public-corpus scope value hybrid_retrieve expects).
_REQUIRED_NONEMPTY_STRING_KEYS: tuple[str, ...] = ("case_id", "query", "ticker")


class GoldenSetError(ValueError):
    """Raised for any structural problem in a golden-set file.

    Every validation pass accumulates ALL offending cases into a single
    message rather than raising on the first problem, so an operator
    refreshing the fixture sees every issue in one pass instead of fixing
    one error at a time.
    """


@dataclass(frozen=True)
class GoldenCase:
    """One golden-set evaluation case.

    Frozen + a tuple (not list) `reference_contexts` field so a scoring loop
    cannot mutate a case in place.

    Fields line up with `hybrid_retrieve(query, user_id, top_k)`'s parameter
    names (app/ingestion/retriever.py) so a case can be fed directly into
    retrieval: `user_id` is REQUIRED scope per INGEST-03 — golden-set cases
    target the public corpus, so it is always the empty string.
    """

    case_id: str
    query: str
    ticker: str
    user_id: str
    provenance: str
    reference_contexts: tuple[str, ...]


def _validate_case(index: int, raw: Any) -> list[str]:
    """Validate a single raw case object, returning a list of problem strings.

    Never raises — all problems for this one case are collected and
    returned so the caller can accumulate across the whole file.
    """
    problems: list[str] = []

    if not isinstance(raw, dict):
        problems.append(f"case at index {index} is not a JSON object")
        return problems

    missing_keys = [key for key in _REQUIRED_KEYS if key not in raw]
    if missing_keys:
        problems.append(
            f"case at index {index} is missing required key(s): "
            f"{', '.join(sorted(missing_keys))}"
        )
        # Without the required keys present, further checks below would
        # raise KeyError rather than accumulate — stop here for this case.
        return problems

    case_id = raw["case_id"]
    label = case_id if isinstance(case_id, str) and case_id else f"index {index}"

    for key in _REQUIRED_NONEMPTY_STRING_KEYS:
        value = raw[key]
        if not isinstance(value, str) or not value:
            problems.append(
                f"case '{label}' field '{key}' must be a non-empty string, got {value!r}"
            )

    user_id = raw["user_id"]
    if not isinstance(user_id, str):
        problems.append(
            f"case '{label}' field 'user_id' must be a string (may be empty), got {user_id!r}"
        )

    provenance = raw["provenance"]
    if not isinstance(provenance, str) or provenance not in ALLOWED_PROVENANCE:
        allowed = ", ".join(sorted(ALLOWED_PROVENANCE))
        problems.append(
            f"case '{label}' field 'provenance' must be one of ({allowed}), got {provenance!r}"
        )

    reference_contexts = raw["reference_contexts"]
    if not isinstance(reference_contexts, list) or not reference_contexts:
        problems.append(
            f"case '{label}' field 'reference_contexts' must be a non-empty list of strings"
        )
    else:
        for context_index, context in enumerate(reference_contexts):
            if not isinstance(context, str):
                problems.append(
                    f"case '{label}' reference_contexts[{context_index}] must be a string"
                )
                continue
            if len(context) < MIN_REFERENCE_CONTEXT_CHARS:
                problems.append(
                    f"case '{label}' reference_contexts[{context_index}] is "
                    f"{len(context)} chars, below the required minimum of "
                    f"{MIN_REFERENCE_CONTEXT_CHARS} chars (a keyword-shaped "
                    "passage cannot cross RAGAS's Levenshtein-similarity "
                    "threshold — replace this entry with real, multi-sentence "
                    "passage text, e.g. copied verbatim from the filing)"
                )

    return problems


def load_golden_set(path: Path | None = None) -> list[GoldenCase]:
    """Load, validate, and return the golden set as a list of `GoldenCase`.

    Args:
        path: Optional explicit path to a golden-set JSON file. Defaults to
              the shipped fixture at `GOLDEN_SET_PATH`.

    Raises:
        GoldenSetError: if the file's top level is not a JSON list, if any
            case is malformed (missing/wrong-typed keys, disallowed
            provenance, an empty or too-short reference_contexts entry), if
            `case_id` values are duplicated, or if the case count falls
            outside `MIN_CASES..MAX_CASES`. All offending cases are named in
            a single accumulated message.
    """
    target_path = path or GOLDEN_SET_PATH
    raw_text = target_path.read_text(encoding="utf-8")
    data = json.loads(raw_text)

    if not isinstance(data, list):
        raise GoldenSetError(
            f"golden-set file {target_path} must contain a top-level JSON "
            f"list, got {type(data).__name__}"
        )

    problems: list[str] = []
    for index, raw_case in enumerate(data):
        problems.extend(_validate_case(index, raw_case))

    # Duplicate case_id detection — only meaningful across cases that were
    # individually well-formed enough to have a usable case_id.
    seen_case_ids: dict[str, int] = {}
    duplicate_ids: set[str] = set()
    for index, raw_case in enumerate(data):
        if not isinstance(raw_case, dict):
            continue
        case_id = raw_case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            continue
        if case_id in seen_case_ids:
            duplicate_ids.add(case_id)
        else:
            seen_case_ids[case_id] = index
    if duplicate_ids:
        problems.append(
            f"duplicate case_id value(s) found: {', '.join(sorted(duplicate_ids))}"
        )

    if not (MIN_CASES <= len(data) <= MAX_CASES):
        problems.append(
            f"golden set has {len(data)} case(s); must contain between "
            f"{MIN_CASES} and {MAX_CASES} cases"
        )

    if problems:
        raise GoldenSetError(
            f"golden-set file {target_path} failed validation with "
            f"{len(problems)} problem(s):\n- " + "\n- ".join(problems)
        )

    return [
        GoldenCase(
            case_id=raw_case["case_id"],
            query=raw_case["query"],
            ticker=raw_case["ticker"],
            user_id=raw_case["user_id"],
            provenance=raw_case["provenance"],
            reference_contexts=tuple(raw_case["reference_contexts"]),
        )
        for raw_case in data
    ]
