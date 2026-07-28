"""Watchlist alert-rule config validation and WATCH-02 latest-memo lookup.

Leaf service module (mirrors ``app/services/company_service.py``'s stated
convention): imports nothing from any other module under ``app/services/``.

Owns the single definition of what a valid ``alert_rules.config`` JSON
payload is for each ``AlertRuleType`` member (D-05's ``config`` column has
no schema of its own — this function is the only gate between a client's
request body and that schemaless column). Per D-10, no baseline price is
stored here: a ``PRICE_MOVE`` config carries only a threshold and a
direction; the reference price it is evaluated against is runtime state
Phase 11's evaluator owns and updates, not something this phase persists.

Also implements D-13's "latest research status" lookup: for a set of
watchlisted tickers, resolve each one's most recent non-deleted
``ResearchMemo`` belonging to the requesting user only (T-10-03-MEMOLEAK) —
a ticker's ownership boundary here is the requesting user, never any other
user's research on the same ticker.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AlertRuleType, ResearchMemo

#: D-09 — the closed vocabulary a PRICE_MOVE config's "direction" key must
#: be a member of. Shared with plan 10-05's router and Phase 11's evaluator.
PRICE_MOVE_DIRECTIONS: tuple[str, ...] = ("up", "down", "either")

#: D-11 — the closed vocabulary a SCHEDULED config's "cadence" key must be
#: a member of. Fixed presets only, no cron parsing anywhere in this phase.
SCHEDULED_CADENCES: tuple[str, ...] = ("daily", "weekly", "monthly")

#: threshold_pct must be strictly greater than this (a zero or negative
#: threshold would fire on every evaluation).
MIN_THRESHOLD_PCT: float = 0.0

#: ...and at most this, inclusive (a threshold above 100 is meaningless as
#: a percentage move gate).
MAX_THRESHOLD_PCT: float = 100.0

#: Required config keys per rule_type (D-09/D-11/D-12). A supplied config's
#: key set must be exactly equal to this, never a superset — an
#: unrecognised extra key would otherwise be silently persisted into the
#: schemaless ``alert_rules.config`` JSON column (T-10-03-CONFIG).
_REQUIRED_KEYS: dict[AlertRuleType, frozenset[str]] = {
    AlertRuleType.NEW_FILING: frozenset(),
    AlertRuleType.PRICE_MOVE: frozenset({"threshold_pct", "direction"}),
    AlertRuleType.SCHEDULED: frozenset({"cadence"}),
}


class InvalidRuleConfigError(ValueError):
    """Raised when an ``alert_rules.config`` payload fails validation.

    The message names only the offending config key(s) and/or the allowed
    vocabulary — never row identifiers or other internal state
    (T-10-03-ERRMSG). Plan 10-05's router maps this to an HTTP 400.
    """


def validate_rule_config(rule_type: AlertRuleType, config: object) -> dict[str, Any]:
    """Validate *config* against the shape required for *rule_type*.

    Pure function — no I/O, no session parameter. Returns a NEW, normalised
    dict (never the caller's object): ``{}`` for ``NEW_FILING``,
    ``{"threshold_pct": <float>, "direction": <str>}`` for ``PRICE_MOVE``,
    ``{"cadence": <str>}`` for ``SCHEDULED``.

    Raises:
        InvalidRuleConfigError: if *config* is not a dict, carries a key set
            other than the exact required set for *rule_type*
            (T-10-03-CONFIG), names an ``AlertRuleType`` member this
            function does not know about (T-10-03-ENUMDRIFT), or (for
            ``PRICE_MOVE``/``SCHEDULED``) carries a value that fails the
            range/vocabulary checks below (T-10-03-NUM).
    """
    if not isinstance(config, dict):
        raise InvalidRuleConfigError(
            f"config must be a JSON object, got {type(config).__name__}"
        )

    required = _REQUIRED_KEYS.get(rule_type)
    if required is None:
        # An AlertRuleType member with no validation branch must fail
        # loudly rather than silently accepting anything (T-10-03-ENUMDRIFT).
        raise InvalidRuleConfigError(f"unknown rule_type: {rule_type!r}")

    supplied = set(config.keys())
    if supplied != required:
        missing = required - supplied
        extra = supplied - required
        parts: list[str] = []
        if missing:
            parts.append(f"missing key(s) {sorted(missing)}")
        if extra:
            parts.append(f"unrecognised key(s) {sorted(extra)}")
        raise InvalidRuleConfigError(
            f"invalid config for {rule_type.value}: {'; '.join(parts)}"
        )

    if rule_type is AlertRuleType.NEW_FILING:
        return {}

    if rule_type is AlertRuleType.PRICE_MOVE:
        raw_threshold = config["threshold_pct"]
        # bool is a subclass of int in Python (isinstance(True, int) is
        # True) — the bool check MUST run before the int/float check, or a
        # boolean threshold would silently pass as 0.0/1.0.
        if isinstance(raw_threshold, bool) or not isinstance(
            raw_threshold, (int, float)
        ):
            raise InvalidRuleConfigError("threshold_pct must be a number")
        threshold_pct = float(raw_threshold)
        if not math.isfinite(threshold_pct):
            # NaN/infinity survive a JSON round trip in Python and would
            # make Phase 11's comparison logic undefined.
            raise InvalidRuleConfigError("threshold_pct must be finite")
        if not (MIN_THRESHOLD_PCT < threshold_pct <= MAX_THRESHOLD_PCT):
            raise InvalidRuleConfigError(
                f"threshold_pct must be in ({MIN_THRESHOLD_PCT}, {MAX_THRESHOLD_PCT}]"
            )
        direction = config["direction"]
        if direction not in PRICE_MOVE_DIRECTIONS:
            raise InvalidRuleConfigError(
                f"direction must be one of {PRICE_MOVE_DIRECTIONS}"
            )
        return {"threshold_pct": threshold_pct, "direction": direction}

    if rule_type is AlertRuleType.SCHEDULED:
        cadence = config["cadence"]
        if cadence not in SCHEDULED_CADENCES:
            raise InvalidRuleConfigError(
                f"cadence must be one of {SCHEDULED_CADENCES}"
            )
        return {"cadence": cadence}

    # Unreachable given _REQUIRED_KEYS above (every branch that passes the
    # key-set check is one of the three known members) — kept as a final
    # guard so a partially-added enum member can never fall through to an
    # implicit accept.
    raise InvalidRuleConfigError(f"unknown rule_type: {rule_type!r}")


async def latest_memo_status_by_ticker(
    tickers: Sequence[str],
    user_id: UUID,
    session: AsyncSession,
) -> dict[str, tuple[str, str]]:
    """Resolve each ticker's latest ResearchMemo status/date for *user_id* (D-13).

    Returns ``{ticker: (status_value, created_at_isoformat)}`` where
    ``status_value`` is a ``ResearchMemoStatus`` member's ``.value`` string
    (so the frontend's existing ``labels.ts::statusBadge`` map applies
    unchanged) and the date is ``created_at.isoformat()``. A ticker with no
    matching memo is simply absent from the returned dict — plan 10-05
    renders that absence as the D-13 "no research yet" state.

    Issues exactly one query (or none, when *tickers* is empty) regardless
    of how many tickers are supplied, avoiding an N+1 pattern in plan
    10-05's ``GET /watchlist`` handler. The query filters on
    ``ResearchMemo.user_id == user_id`` — mandatory, not an optimisation:
    without it, one user's watchlist would surface another user's memo
    status and research date for the same ticker (T-10-03-MEMOLEAK) — and
    on ``ResearchMemo.deleted_at.is_(None)``, which excludes soft-deleted
    memos from the WATCH-02 display.
    """
    if not tickers:
        return {}

    result = await session.execute(
        select(ResearchMemo)
        .where(
            ResearchMemo.ticker.in_(tickers),
            ResearchMemo.user_id == user_id,
            ResearchMemo.deleted_at.is_(None),
        )
        .order_by(ResearchMemo.created_at.desc())
    )
    rows = result.scalars().all()

    latest: dict[str, tuple[str, str]] = {}
    for memo in rows:
        if memo.ticker is None or memo.ticker in latest:
            # Rows arrive newest-first; the first row seen per ticker wins,
            # so a later (older) row for an already-resolved ticker is
            # skipped rather than overwriting it.
            continue
        latest[memo.ticker] = (memo.status.value, memo.created_at.isoformat())
    return latest
