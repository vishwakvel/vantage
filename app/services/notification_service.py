"""User-scoped query helpers backing WATCH-06 (notifications) and WATCH-07
(per-ticker alert history).

``alert_events`` has no ``user_id`` column by design (plan 11-01,
T-11-01-OWNCHAIN) — EVERY function in this module derives ownership by
joining ``AlertEvent -> AlertRule -> WatchlistEntry`` and filtering on
``WatchlistEntry.user_id``. This module is the single source of the
snapshot shape shared by the REST route (``app/api/v1/notifications.py``)
and the WebSocket route (``app/api/v1/ws.py::notifications_ws``), so the
two can never drift apart.

This is a leaf service in the ``app/services/watchlist_service.py`` sense:
every function takes ``session: AsyncSession`` as a plain parameter (never
a FastAPI ``Depends``), imports nothing HTTP-specific, and raises nothing
mapped to an HTTP status code — the caller (a route handler) is responsible
for mapping absence (e.g. an unowned or unknown id) to a 404.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AlertEvent, AlertRule, WatchlistEntry
from app.services.notification_publisher import notification_payload

#: D-08: a low-frequency, occasional-check-in feature does not warrant
#: pagination for v2.0 — if a user ever accumulates enough history to need
#: it, add real pagination then (YAGNI).
DEFAULT_EVENT_LIMIT: int = 50


async def recent_events_for_user(
    user_id: UUID,
    session: AsyncSession,
    limit: int = DEFAULT_EVENT_LIMIT,
) -> list[AlertEvent]:
    """Return *user_id*'s most recent alert events, newest first.

    Joins ``AlertEvent -> AlertRule -> WatchlistEntry`` and filters on the
    parent entry's owning user (mandatory, not an optimisation — without it
    this would return every user's events). Capped at *limit* rows; the
    bell/dropdown (D-10) only ever needs the most recent window.
    """
    result = await session.execute(
        select(AlertEvent)
        .join(AlertRule, AlertEvent.alert_rule_id == AlertRule.id)
        .join(WatchlistEntry, AlertRule.watchlist_id == WatchlistEntry.id)
        .where(WatchlistEntry.user_id == user_id)
        .order_by(AlertEvent.triggered_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def unread_count_for_user(user_id: UUID, session: AsyncSession) -> int:
    """Return the TOTAL count of *user_id*'s unread events.

    Intentionally NOT limited to ``DEFAULT_EVENT_LIMIT`` — the bell badge
    must show the true unread total even when more than
    ``DEFAULT_EVENT_LIMIT`` events are unread, not just the count within the
    returned window.
    """
    result = await session.execute(
        select(func.count())
        .select_from(AlertEvent)
        .join(AlertRule, AlertEvent.alert_rule_id == AlertRule.id)
        .join(WatchlistEntry, AlertRule.watchlist_id == WatchlistEntry.id)
        .where(WatchlistEntry.user_id == user_id, AlertEvent.read.is_(False))
    )
    return result.scalar_one()


async def mark_all_read_for_user(user_id: UUID, session: AsyncSession) -> int:
    """Mark every unread event owned by *user_id* read in ONE bulk UPDATE.

    Returns the affected row count; a second call for the same user returns
    0. This must never become a per-row Python loop (D-10's bulk "opening
    the dropdown marks all listed notifications read" action) — the
    ``owned_unread_ids`` subquery restricting ids to the caller's own is not
    an optimisation: without it, this UPDATE would mark every user's
    notifications read (T-11-04-BULK).

    Uses ``synchronize_session="fetch"`` (not ``False``): this still issues
    exactly ONE UPDATE statement (plus one SELECT to gather the matching
    ids for in-session sync — not a per-row loop), but keeps any
    already-loaded ``AlertEvent`` ORM objects in *this* session's identity
    map consistent with the row just written. Without it, a caller that
    re-reads events through the SAME session immediately after calling this
    (e.g. ``recent_events_for_user`` within one request/test) would see the
    stale pre-update ``read`` value from the identity map even though the
    database row itself is already updated — this exact staleness was
    caught by ``tests/api/test_notifications_api.py::test_round_trip_notification_then_history``.
    """
    owned_unread_ids = (
        select(AlertEvent.id)
        .join(AlertRule, AlertEvent.alert_rule_id == AlertRule.id)
        .join(WatchlistEntry, AlertRule.watchlist_id == WatchlistEntry.id)
        .where(WatchlistEntry.user_id == user_id, AlertEvent.read.is_(False))
        .scalar_subquery()
    )
    result = await session.execute(
        update(AlertEvent)
        .where(AlertEvent.id.in_(owned_unread_ids))
        .values(read=True)
        .execution_options(synchronize_session="fetch")
    )
    await session.commit()
    return result.rowcount


async def recent_events_for_entry(
    entry_id: UUID,
    session: AsyncSession,
    limit: int = DEFAULT_EVENT_LIMIT,
) -> list[AlertEvent]:
    """Return the most recent alert events for one ``WatchlistEntry``'s rules.

    This function does NOT check ownership — the caller MUST verify the
    ``WatchlistEntry`` belongs to the requesting user before calling it
    (see ``app/api/v1/notifications.py::get_alert_history``).

    Deliberately NOT filtered by the parent rule's current ``enabled`` value
    (D-07): disabling a rule is a soft toggle (WATCH-08), and a trigger that
    really happened is not retroactively invalidated by later switching off
    the rule that produced it.
    """
    result = await session.execute(
        select(AlertEvent)
        .join(AlertRule, AlertEvent.alert_rule_id == AlertRule.id)
        .where(AlertRule.watchlist_id == entry_id)
        .order_by(AlertEvent.triggered_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


def event_to_payload(event: AlertEvent) -> dict[str, Any]:
    """Map an ``AlertEvent`` row to the canonical notification dict shape.

    Delegates to ``notification_publisher.notification_payload`` so the key
    set (``id``, ``alert_rule_id``, ``message``, ``triggered_at``, ``read``)
    has exactly one definition shared by the evaluator's WS push, the REST
    ``NotificationResponse`` model, and the frontend's ``NotificationEntry``
    interface.
    """
    return notification_payload(
        event_id=str(event.id),
        alert_rule_id=str(event.alert_rule_id),
        message=event.message,
        triggered_at=event.triggered_at.isoformat(),
        read=bool(event.read),
    )


__all__ = [
    "DEFAULT_EVENT_LIMIT",
    "recent_events_for_user",
    "unread_count_for_user",
    "mark_all_read_for_user",
    "recent_events_for_entry",
    "event_to_payload",
]
