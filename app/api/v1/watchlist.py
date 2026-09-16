"""Watchlist and alert-rule API endpoints — the five routes backing Phase 10.

Endpoints:
- POST /watchlist → 200 WatchlistEntryResponse (ticker added, idempotently
  per D-04) or 400 (malformed ticker)
- GET /watchlist → 200 WatchlistResponse (every entry with nested alert
  rules and the D-13 latest-memo status, in one response)
- DELETE /watchlist/{entry_id} → 204 no body (entry and its alert rules
  removed via the D-03 cascade) or 404 (not found or not owned)
- POST /watchlist/{entry_id}/rules → 200 AlertRuleResponse (rule created)
  or 400 (invalid rule config) or 404 (entry not found or not owned)
- PATCH /watchlist/rules/{rule_id} → 200 AlertRuleResponse (enabled flag
  flipped) or 404 (rule not found or not owned)

Mounted under /api/v1 by the v1 aggregator, yielding:
  /api/v1/watchlist
  /api/v1/watchlist/{entry_id}
  /api/v1/watchlist/{entry_id}/rules
  /api/v1/watchlist/rules/{rule_id}

Implements WATCH-01 (add/remove any ticker, including brand-new ones, per
D-01/D-02/D-04), WATCH-02 (nested watchlist read with D-13 latest-memo
status), WATCH-03/04/05 (NEW_FILING/PRICE_MOVE/SCHEDULED rule creation with
server-validated configs per D-09/D-11/D-12), and WATCH-08 (per-rule
enable/disable, D-07/D-08 — no rule-delete route and no watchlist-level
toggle exist anywhere in this module), per 10-CONTEXT.md.

Security boundaries (STRIDE T-10-05-IDOR, T-10-05-AUTHZ, T-10-05-TICKER,
T-10-05-CONFIG, T-10-05-MEMOLEAK):
- T-10-05-AUTHZ: every handler sources user identity ONLY from
  ``Depends(get_current_user)``; no route accepts a ``user_id`` path or
  body field. An unauthenticated request is rejected before any handler
  body runs.
- T-10-05-IDOR: every entry-scoped route filters on
  ``WatchlistEntry.user_id == user.id``, and the rule-scoped route joins
  ``AlertRule`` to its parent ``WatchlistEntry`` on the same predicate. A
  non-owned or non-existent id returns 404 in every case — never 403, so
  the response can never be used as an existence oracle.
- T-10-05-TICKER: the ticker is trimmed, uppercased, and matched against
  ``_TICKER_RE`` before it reaches ``ensure_company_exists`` or any insert
  — D-02 removes only the market-data validation, not this charset/length
  bound.
- T-10-05-CONFIG: ``rule_type`` is typed as ``AlertRuleType`` (Pydantic
  rejects an unknown member with 422 before any DB work), and ``config``
  is validated by ``watchlist_service.validate_rule_config`` before any
  write — the NORMALISED return value is what gets stored, never the raw
  request body.
- T-10-05-MEMOLEAK: the D-13 latest-memo lookup is delegated to
  ``watchlist_service.latest_memo_status_by_ticker(tickers, user.id,
  session)``, which filters on ``ResearchMemo.user_id`` — watchlisting a
  popular ticker can never disclose another user's research on it.
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user, get_session
from app.db.models import AlertRule, AlertRuleType, User, WatchlistEntry
from app.services.company_service import ensure_company_exists
from app.services.watchlist_service import (
    InvalidRuleConfigError,
    latest_memo_status_by_ticker,
    validate_rule_config,
)

router = APIRouter(prefix="/watchlist", tags=["watchlist"])

#: Identical contract to app/api/v1/ingest.py and app/api/v1/research.py's
#: _TICKER_RE — 1-10 uppercase alphanumeric characters. D-02 rules out
#: market-data validation (no yfinance existence check) but not this
#: charset/length bound; the column is a foreign key into companies.ticker.
_TICKER_RE: re.Pattern[str] = re.compile(r"^[A-Z0-9]{1,10}$")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AddTickerBody(BaseModel):
    """Request body for POST /watchlist (WATCH-01).

    ``ticker`` is bounded here only to reject an absurdly long string
    before the handler body runs; the real charset/length contract
    (``_TICKER_RE``) is enforced after normalisation inside the handler,
    matching the house convention already used in ``ingest.py``.
    """

    ticker: str = Field(max_length=32)


class CreateAlertRuleBody(BaseModel):
    """Request body for POST /watchlist/{entry_id}/rules (WATCH-03/04/05).

    ``rule_type`` is typed as ``AlertRuleType`` so Pydantic rejects an
    unknown member with 422 before any DB work; ``config`` is the raw,
    not-yet-validated payload passed to
    ``watchlist_service.validate_rule_config``.
    """

    rule_type: AlertRuleType
    config: dict


class ToggleAlertRuleBody(BaseModel):
    """Request body for PATCH /watchlist/rules/{rule_id} (WATCH-08)."""

    enabled: bool


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AlertRuleResponse(BaseModel):
    """One alert rule attached to a watchlist entry (WATCH-03/04/05/08)."""

    id: str
    rule_type: str
    config: dict
    enabled: bool
    created_at: str


class WatchlistEntryResponse(BaseModel):
    """One watchlisted ticker with its nested rules and D-13 status (WATCH-01/02)."""

    id: str
    ticker: str
    created_at: str
    latest_memo_status: str | None
    latest_memo_date: str | None
    alert_rules: list[AlertRuleResponse]


class WatchlistResponse(BaseModel):
    """Returned by GET /watchlist (WATCH-02)."""

    entries: list[WatchlistEntryResponse]


def _alert_rule_to_response(rule: AlertRule) -> AlertRuleResponse:
    """Map an ``AlertRule`` row to its response model.

    Shared by every handler that returns a rule (create, toggle, and the
    nested list inside GET /watchlist) so the three call sites cannot
    drift apart.
    """
    return AlertRuleResponse(
        id=str(rule.id),
        rule_type=rule.rule_type.value,
        config=rule.config,
        enabled=rule.enabled,
        created_at=rule.created_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@router.post("", response_model=WatchlistEntryResponse)
async def add_watchlist_entry(
    body: AddTickerBody,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WatchlistEntryResponse:
    """Add a ticker to the authenticated user's watchlist (WATCH-01).

    Accepts any ticker the user types, including one Vantage has never
    researched (D-01/D-02) — the ``Company`` parent row is upserted before
    the watchlist insert so a brand-new ticker never raises a
    ``ForeignKeyViolationError``. A repeat add for a ticker already on the
    list is a no-op that returns the existing row rather than an error
    (D-04); two concurrent adds for the same ticker cannot race into an
    ``IntegrityError``.

    Args:
        body:    Request body carrying the raw, not-yet-normalised ticker.
        user:    Authenticated user resolved from the Bearer JWT
                 (T-10-05-AUTHZ); the ONLY source of user identity.
        session: Injected async DB session.

    Returns:
        ``WatchlistEntryResponse`` for the (possibly pre-existing) entry.
        A newly added entry always has an empty ``alert_rules`` list and
        ``None`` for both latest-memo fields — the D-13 lookup is not run
        here; the frontend refetches the full list after an add.

    Raises:
        HTTPException: 400 if the normalised ticker does not match
                        ``_TICKER_RE``.
    """
    ticker = body.ticker.strip().upper()
    if not _TICKER_RE.match(ticker):
        raise HTTPException(
            status_code=400,
            detail="ticker must be 1-10 uppercase alphanumeric characters",
        )

    # D-01: upsert the Company parent row BEFORE the watchlist insert — the
    # PROJECT.md Company-row FK gap this phase closes for Watchlist.
    await ensure_company_exists(ticker, session)

    # D-04: idempotent add via ON CONFLICT DO NOTHING against
    # uq_watchlist_entries_user_id_ticker — a repeat add is a no-op, and two
    # concurrent adds for the same ticker cannot race into an IntegrityError.
    stmt = (
        pg_insert(WatchlistEntry)
        .values(user_id=user.id, ticker=ticker)
        .on_conflict_do_nothing(index_elements=["user_id", "ticker"])
    )
    await session.execute(stmt)
    await session.commit()

    result = await session.execute(
        select(WatchlistEntry).where(
            WatchlistEntry.user_id == user.id, WatchlistEntry.ticker == ticker
        )
    )
    entry = result.scalar_one()

    return WatchlistEntryResponse(
        id=str(entry.id),
        ticker=entry.ticker,
        created_at=entry.created_at.isoformat(),
        latest_memo_status=None,
        latest_memo_date=None,
        alert_rules=[],
    )


@router.get("", response_model=WatchlistResponse)
async def list_watchlist(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WatchlistResponse:
    """List the authenticated user's watchlist with nested rules (WATCH-02).

    Each entry carries its D-13 latest-memo status/date (or ``None`` for
    "no research yet") and every ``AlertRule`` attached to it. Issues
    exactly one entries query, one memo-status call, and one rules query —
    never a per-entry query loop.

    Args:
        user:    Authenticated user resolved from the Bearer JWT
                 (T-10-05-AUTHZ); the ONLY source of user identity.
        session: Injected async DB session.

    Returns:
        ``WatchlistResponse`` with entries ordered by ticker ascending. An
        empty ``entries`` list (never a 404) when the user watches
        nothing.
    """
    entries_result = await session.execute(
        select(WatchlistEntry)
        .where(WatchlistEntry.user_id == user.id)
        .order_by(WatchlistEntry.ticker.asc())
    )
    entries = entries_result.scalars().all()

    if not entries:
        return WatchlistResponse(entries=[])

    tickers = [entry.ticker for entry in entries]
    # T-10-05-MEMOLEAK: filters on ResearchMemo.user_id internally, so this
    # can never surface another user's research on the same ticker.
    memo_status = await latest_memo_status_by_ticker(tickers, user.id, session)

    entry_ids = [entry.id for entry in entries]
    rules_result = await session.execute(
        select(AlertRule)
        .where(AlertRule.watchlist_id.in_(entry_ids))
        .order_by(AlertRule.created_at.asc())
    )
    rules_by_entry: dict[uuid.UUID, list[AlertRule]] = {}
    for rule in rules_result.scalars().all():
        rules_by_entry.setdefault(rule.watchlist_id, []).append(rule)

    responses: list[WatchlistEntryResponse] = []
    for entry in entries:
        status_and_date = memo_status.get(entry.ticker)
        responses.append(
            WatchlistEntryResponse(
                id=str(entry.id),
                ticker=entry.ticker,
                created_at=entry.created_at.isoformat(),
                latest_memo_status=(status_and_date[0] if status_and_date else None),
                latest_memo_date=(status_and_date[1] if status_and_date else None),
                alert_rules=[
                    _alert_rule_to_response(rule) for rule in rules_by_entry.get(entry.id, [])
                ],
            )
        )

    return WatchlistResponse(entries=responses)


# Declared ABOVE the /{entry_id}-shaped routes below so the literal "rules"
# path segment is registered before it could ever be swallowed as a path
# parameter (no collision today since the methods differ, but the ordering
# costs nothing and removes a latent trap).
@router.patch("/rules/{rule_id}", response_model=AlertRuleResponse)
async def set_alert_rule_enabled(
    rule_id: uuid.UUID,
    body: ToggleAlertRuleBody,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AlertRuleResponse:
    """Flip an alert rule's enabled flag without deleting it (WATCH-08).

    Ownership is enforced by joining ``AlertRule`` to its parent
    ``WatchlistEntry`` and filtering on the parent's ``user_id``
    (T-10-05-IDOR) — a rule id belonging to another user's watchlist
    returns 404, identical to a non-existent id. Only the ``enabled``
    column is writable through this route; ``rule_type`` and ``config``
    are immutable after creation, and D-07 means no sibling delete route
    exists anywhere in this module.

    Args:
        rule_id: AlertRule UUID from the URL path.
        body:    Request body carrying the new ``enabled`` value.
        user:    Authenticated user resolved from the Bearer JWT
                 (T-10-05-AUTHZ); the ONLY source of user identity.
        session: Injected async DB session.

    Returns:
        ``AlertRuleResponse`` for the updated rule.

    Raises:
        HTTPException: 404 if the rule does not exist or is not owned
                        (via its parent watchlist entry) by ``user``.
    """
    result = await session.execute(
        select(AlertRule)
        .join(WatchlistEntry, AlertRule.watchlist_id == WatchlistEntry.id)
        .where(AlertRule.id == rule_id, WatchlistEntry.user_id == user.id)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Alert rule not found")

    rule.enabled = body.enabled
    await session.commit()
    await session.refresh(rule)

    return _alert_rule_to_response(rule)


@router.delete("/{entry_id}", status_code=204)
async def remove_watchlist_entry(
    entry_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Remove a ticker from the authenticated user's watchlist (WATCH-01).

    Ownership is checked against ``WatchlistEntry.user_id == user.id``
    (T-10-05-IDOR) — a non-owned or non-existent ``entry_id`` returns 404
    in both cases, never 403. Deleting the entry cascades to its
    ``AlertRule`` rows via the D-03 ``ondelete="CASCADE"`` FK, so no
    separate rule cleanup is needed here. ``entry_id`` is declared as
    ``uuid.UUID`` so a malformed identifier is answered with 422 by
    FastAPI before the query ever runs.

    Args:
        entry_id: WatchlistEntry UUID from the URL path.
        user:     Authenticated user resolved from the Bearer JWT
                  (T-10-05-AUTHZ); the ONLY source of user identity.
        session:  Injected async DB session.

    Returns:
        A bare 204 ``Response`` with no body.

    Raises:
        HTTPException: 404 if the entry does not exist or is not owned by
                        ``user``.
    """
    result = await session.execute(
        select(WatchlistEntry).where(
            WatchlistEntry.id == entry_id, WatchlistEntry.user_id == user.id
        )
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Watchlist entry not found")

    await session.delete(entry)
    await session.commit()

    return Response(status_code=204)


@router.post("/{entry_id}/rules", response_model=AlertRuleResponse)
async def create_alert_rule(
    entry_id: uuid.UUID,
    body: CreateAlertRuleBody,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AlertRuleResponse:
    """Create a new alert rule on a watchlisted ticker (WATCH-03/04/05).

    Ownership of the parent entry is checked exactly as in
    ``remove_watchlist_entry`` (T-10-05-IDOR) before any rule work happens.
    ``config`` is validated by ``watchlist_service.validate_rule_config``
    (T-10-05-CONFIG); an invalid config is rejected with 400 naming only
    the offending config key(s) and/or allowed vocabulary, never a row
    identifier. The rule is stored with the NORMALISED config the
    validator returned, never the raw request body. D-06 permits several
    rules of the same type on one entry — no duplicate check is performed.

    Args:
        entry_id: WatchlistEntry UUID from the URL path.
        body:     Request body carrying ``rule_type`` and the raw
                   ``config``.
        user:     Authenticated user resolved from the Bearer JWT
                   (T-10-05-AUTHZ); the ONLY source of user identity.
        session:  Injected async DB session.

    Returns:
        ``AlertRuleResponse`` for the newly created rule.

    Raises:
        HTTPException: 404 if the entry does not exist or is not owned by
                        ``user``; 400 if ``config`` fails validation for
                        ``rule_type``.
    """
    result = await session.execute(
        select(WatchlistEntry).where(
            WatchlistEntry.id == entry_id, WatchlistEntry.user_id == user.id
        )
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Watchlist entry not found")

    try:
        normalised_config = validate_rule_config(body.rule_type, body.config)
    except InvalidRuleConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rule = AlertRule(
        watchlist_id=entry.id,
        rule_type=body.rule_type,
        config=normalised_config,
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule)

    return _alert_rule_to_response(rule)
