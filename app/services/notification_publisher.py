"""Live-notification emit side (D-09) — Redis pub/sub channel + event contract.

This module is the single source of truth for:
- the per-user notification channel name (``notification_channel``)
- the shape of the notification payload envelope (``notification_payload``)

Both the publish side (plan 11-03's alert evaluator) and the subscribe side
(plan 11-04's ``/ws/notifications`` route) must derive the channel via
``notification_channel`` and build the notification body via
``notification_payload`` — never re-format the channel string or the payload
dict inline, so publisher and subscriber can never drift apart.

Unlike the per-memo progress channel (``progress_publisher.py``), this
channel has no terminal event: a subscriber stays connected for the whole
session, not just for the duration of one research run.

A publish reaching zero subscribers (no WebSocket client currently
listening) is a normal outcome, not an error — the durable ``alert_events``
row (D-09) is the source of truth for a notification's existence; the
WebSocket push is best-effort delivery on top of it.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from app.core.config import Settings, get_settings


def notification_channel(user_id: str) -> str:
    """Return the deterministic per-user Redis pub/sub channel name.

    This is the single source of truth for the channel name — both the
    publish side (this module) and the subscribe side
    (``app/api/v1/ws.py``'s ``/ws/notifications`` route) must derive the
    channel via this function, never by re-formatting the string inline.
    """
    return f"notifications:{user_id}"


def _redis(settings: Settings) -> aioredis.Redis:
    """Return a plain (non-``Depends``) Redis client from ``settings.REDIS_URL``.

    Identical call shape to ``app.services.progress_publisher._redis``, but
    callable outside FastAPI dependency injection — the alert evaluator runs
    inside a long-lived Celery beat worker process, entirely outside any
    request context.
    """
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


def notification_payload(
    event_id: str,
    alert_rule_id: str,
    message: str,
    triggered_at: str,
    read: bool = False,
) -> dict[str, Any]:
    """Return the notification dict shape shared across publisher/subscriber/frontend.

    This is the ONE definition of the notification dict shape. Plan 11-03's
    evaluator builds WebSocket pushes with it, plan 11-04's
    ``NotificationResponse`` Pydantic model declares the identical field
    names, and plan 11-08's ``NotificationEntry`` TypeScript interface
    mirrors them — a key added here must be added in all three places or the
    frontend silently drops it.

    ``triggered_at`` is always an ISO-8601 string, never a ``datetime``,
    because the value is JSON-serialised for the Redis publish.
    """
    return {
        "id": event_id,
        "alert_rule_id": alert_rule_id,
        "message": message,
        "triggered_at": triggered_at,
        "read": read,
    }


async def publish_notification(
    user_id: str,
    notification: dict[str, Any],
    settings: Settings | None = None,
) -> None:
    """Publish a notification event on the user's notification channel.

    Payload: ``{"type": "notification", **notification}``.

    Deliberate, documented improvement over ``progress_publisher``, which
    creates a Redis client per publish and never closes it: that module runs
    per-HTTP-request, so the connection is reclaimed at request end via
    process/connection-pool teardown. This function instead runs once per
    firing rule inside a long-lived Celery beat worker process — without an
    explicit close, connections would accumulate across ticks rather than
    ever being reclaimed. The client is therefore closed in a ``finally``
    block, including when ``redis.publish`` itself raises.

    A publish reaching zero subscribers (``redis.publish`` returning ``0``)
    is a normal outcome, intentionally ignored here — the durable
    ``alert_events`` row (D-09) is the source of truth for the notification;
    this push is best-effort.
    """
    if settings is None:
        settings = get_settings()
    redis = _redis(settings)
    try:
        payload = {"type": "notification", **notification}
        await redis.publish(notification_channel(user_id), json.dumps(payload))
    finally:
        await redis.close()


__all__ = [
    "notification_channel",
    "notification_payload",
    "publish_notification",
]
