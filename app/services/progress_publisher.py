"""Live-progress emit side (EXEC-01) — Redis pub/sub channel + event contract.

This module is the single source of truth for:
- the per-memo progress channel name (``progress_channel``)
- the shape of every event published on that channel (``publish_agent_status``,
  ``publish_memo_terminal``)

Both the worker (``app/graph/research_graph.py``'s ``_with_progress`` wrapper,
06-02) and the WebSocket route (``app/api/v1/ws.py``, 06-04) import from here
so the channel name and payload shape can never drift between publisher and
subscriber (06-CONTEXT.md D-05, D-06).

Payloads carry ONLY the status-enum transition — no free-text progress detail
(D-06). A publish reaching zero subscribers (no WebSocket client currently
listening) is a normal outcome, not an error: ``redis.publish`` returns the
subscriber count, which callers here intentionally ignore.
"""

from __future__ import annotations

import json

import redis.asyncio as aioredis

from app.core.config import Settings, get_settings


def progress_channel(memo_id: str) -> str:
    """Return the deterministic per-memo Redis pub/sub channel name.

    This is the single source of truth for the channel name — both the
    publish side (this module) and the subscribe side
    (``app/api/v1/ws.py``) must derive the channel via this function, never
    by re-formatting the string inline.
    """
    return f"research:progress:{memo_id}"


def _redis(settings: Settings) -> aioredis.Redis:
    """Return a plain (non-``Depends``) Redis client from ``settings.REDIS_URL``.

    Identical call shape to ``app.core.dependencies.get_redis``, but callable
    outside FastAPI dependency injection — the graph's node-wrapping helper
    and any Celery task run outside a request context.
    """
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def publish_agent_status(
    memo_id: str,
    agent_type: str,
    status: str,
    settings: Settings | None = None,
) -> None:
    """Publish a per-agent status transition on the memo's progress channel.

    Payload: ``{"type": "agent", "agent_type": agent_type, "status": status}``.
    A publish reaching zero subscribers is a normal no-op, not an error.
    """
    if settings is None:
        settings = get_settings()
    redis = _redis(settings)
    payload = {"type": "agent", "agent_type": agent_type, "status": status}
    await redis.publish(progress_channel(memo_id), json.dumps(payload))


async def publish_memo_terminal(
    memo_id: str,
    memo_status: str,
    settings: Settings | None = None,
) -> None:
    """Publish the memo's terminal status on its progress channel.

    Payload: ``{"type": "memo", "status": memo_status}``.
    """
    if settings is None:
        settings = get_settings()
    redis = _redis(settings)
    payload = {"type": "memo", "status": memo_status}
    await redis.publish(progress_channel(memo_id), json.dumps(payload))


__all__ = [
    "progress_channel",
    "publish_agent_status",
    "publish_memo_terminal",
]
