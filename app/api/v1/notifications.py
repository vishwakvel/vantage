"""Notification and per-ticker alert-history API endpoints (WATCH-06, WATCH-07).

Endpoints:
- GET /notifications → 200 NotificationListResponse (the caller's own most
  recent 50 events plus the true unread count; empty list, never 404)
- POST /notifications/mark-read → 200 MarkReadResponse (every unread event
  owned by the caller marked read in one bulk UPDATE, D-10)
- GET /notifications/history/{entry_id} → 200 AlertHistoryResponse (most
  recent 50 events for one owned watchlisted ticker, WATCH-07/D-08) or 404
  (entry not found or not owned) or 422 (malformed id)

Mounted under /api/v1 by the v1 aggregator, yielding:
  /api/v1/notifications
  /api/v1/notifications/mark-read
  /api/v1/notifications/history/{entry_id}

Implements WATCH-06 (durable notification read + unread-count layer) and
WATCH-07 (per-ticker alert history, D-08's most-recent-50-no-pagination
contract), and D-06/D-07/D-10 from 11-CONTEXT.md.

There is deliberately NO request model anywhere in this module: no route
accepts a body, a ``user_id``, or a ``state`` field. ``AlertRule.state`` is
evaluator-owned (D-03) and must remain unwritable over HTTP.

Security boundaries (STRIDE T-11-04-AUTHZ, T-11-04-IDOR, T-11-04-BULK):
- T-11-04-AUTHZ: every handler sources user identity ONLY from
  ``Depends(get_current_user)``; no route declares a ``user_id`` path or
  body parameter, and no route accepts a request body at all.
- T-11-04-IDOR: ``get_alert_history`` resolves ownership via
  ``WatchlistEntry.id == entry_id AND WatchlistEntry.user_id == user.id``
  before any event query; a non-owned or unknown id both return 404 with
  the identical detail string, so the response can never be used as an
  existence oracle. ``entry_id: uuid.UUID`` yields 422 on a malformed id
  before any DB work.
- T-11-04-BULK: ``mark_notifications_read`` delegates to
  ``notification_service.mark_all_read_for_user``, whose ``WHERE`` clause
  is restricted by a ``scalar_subquery`` of ids joined through to
  ``WatchlistEntry.user_id == user.id`` — without it, one user's
  dropdown-open would mark every user's notifications read.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user, get_session
from app.db.models import User, WatchlistEntry
from app.services.notification_service import (
    event_to_payload,
    mark_all_read_for_user,
    recent_events_for_entry,
    recent_events_for_user,
    unread_count_for_user,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class NotificationResponse(BaseModel):
    """One alert event as delivered over REST and WS (WATCH-06/07).

    These five field names are the same set
    ``notification_publisher.notification_payload`` produces and
    ``frontend/src/api.ts``'s ``NotificationEntry`` interface mirrors — a
    field added here must be added in all three places or the frontend
    silently drops it.
    """

    id: str
    alert_rule_id: str
    message: str
    triggered_at: str
    read: bool


class NotificationListResponse(BaseModel):
    """Returned by GET /notifications (WATCH-06)."""

    notifications: list[NotificationResponse]
    unread_count: int


class MarkReadResponse(BaseModel):
    """Returned by POST /notifications/mark-read (D-10)."""

    marked: int


class AlertHistoryResponse(BaseModel):
    """Returned by GET /notifications/history/{entry_id} (WATCH-07/D-08)."""

    ticker: str
    events: list[NotificationResponse]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> NotificationListResponse:
    """Return the caller's most recent 50 events plus the true unread count (WATCH-06).

    Issues exactly two queries total (one for the capped event window, one
    for the true unread count), never a per-event query. An empty result is
    200 with an empty list, never 404 — mirrors ``list_watchlist``'s
    empty-state contract.

    Args:
        user:    Authenticated user resolved from the Bearer JWT
                 (T-11-04-AUTHZ); the ONLY source of user identity.
        session: Injected async DB session.

    Returns:
        ``NotificationListResponse`` with events newest-first and the
        caller's total unread count.
    """
    events = await recent_events_for_user(user.id, session)
    unread_count = await unread_count_for_user(user.id, session)
    return NotificationListResponse(
        notifications=[
            NotificationResponse(**event_to_payload(event)) for event in events
        ],
        unread_count=unread_count,
    )


# Declared BEFORE the /history/{entry_id} route below so the literal
# "mark-read" path segment is registered before any parameterised sibling —
# same defensive ordering comment watchlist.py uses above
# set_alert_rule_enabled.
@router.post("/mark-read", response_model=MarkReadResponse)
async def mark_notifications_read(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MarkReadResponse:
    """Mark every unread event owned by the caller read in one bulk UPDATE (D-10).

    A subsequent GET shows ``unread_count == 0``; a second call to this
    route returns ``{"marked": 0}``. Never touches another user's unread
    events (T-11-04-BULK).

    Args:
        user:    Authenticated user resolved from the Bearer JWT
                 (T-11-04-AUTHZ); the ONLY source of user identity.
        session: Injected async DB session.

    Returns:
        ``MarkReadResponse`` with the count of rows just marked read.
    """
    marked = await mark_all_read_for_user(user.id, session)
    return MarkReadResponse(marked=marked)


@router.get("/history/{entry_id}", response_model=AlertHistoryResponse)
async def get_alert_history(
    entry_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AlertHistoryResponse:
    """Return the most recent 50 alert events for one watchlisted ticker (WATCH-07).

    Ownership is resolved first via ``WatchlistEntry.id == entry_id AND
    WatchlistEntry.user_id == user.id`` (T-11-04-IDOR) — a non-owned or
    unknown id both return 404 with the identical detail string, so the
    response can never be used as an existence oracle (404 never 403).
    ``entry_id`` is declared as ``uuid.UUID`` so a malformed id yields 422
    before any query. Includes events from rules that are currently
    disabled (D-07) — history is not filtered by a rule's ``enabled`` flag.

    Args:
        entry_id: WatchlistEntry UUID from the URL path.
        user:     Authenticated user resolved from the Bearer JWT
                   (T-11-04-AUTHZ); the ONLY source of user identity.
        session:  Injected async DB session.

    Returns:
        ``AlertHistoryResponse`` with the entry's ticker and its events,
        newest first, capped at 50 (D-08).

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

    events = await recent_events_for_entry(entry_id, session)
    return AlertHistoryResponse(
        ticker=entry.ticker,
        events=[NotificationResponse(**event_to_payload(event)) for event in events],
    )


__all__ = [
    "router",
    "NotificationResponse",
    "NotificationListResponse",
    "MarkReadResponse",
    "AlertHistoryResponse",
    "list_notifications",
    "mark_notifications_read",
    "get_alert_history",
]
