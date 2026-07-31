"""Live-progress and live-notification WebSocket routes.

Endpoints:
- WS /ws/research/{memo_id} (EXEC-01, D-08/D-09/D-10) → authenticates via a
  ``?token=`` query-param JWT (browsers cannot set custom headers on a WS
  handshake), enforces memo ownership, sends a snapshot of current
  per-agent statuses, streams live per-agent transitions from Redis pub/sub
  (``app.services.progress_publisher``, 06-02), then sends the terminal
  memo status and closes the socket itself.
- WS /ws/notifications (WATCH-06, D-09) → authenticates the same way,
  subscribes to the authenticated user's own ``notifications:{user_id}``
  channel (``app.services.notification_publisher``, 11-02), sends a
  snapshot of recent events plus the unread count, then forwards every
  subsequent push for the life of the session — this socket never closes
  itself.

Mounted under /api/v1 by the v1 aggregator, yielding:
  /api/v1/ws/research/{memo_id}
  /api/v1/ws/notifications

Security boundaries (STRIDE T-06-01, T-06-02, T-06-03, T-06-04, T-06-04-XCH,
T-11-04-WSAUTH, T-11-04-WSCHAN):
- T-06-01 (spoofing): ``get_current_user_ws`` replicates the EXACT
  validation pipeline as ``app.core.dependencies.get_current_user``
  (decode → sub/jti required → Redis blocklist check → user fetch); any
  failure returns ``None`` rather than raising (this is a WS variant), and
  the caller closes with code 1008 BEFORE ``accept()`` — no channel is ever
  opened for an unauthenticated peer.
- T-06-02 (IDOR / elevation of privilege): ownership is enforced via
  ``ResearchMemo.id == memo_id AND ResearchMemo.user_id == user.id``. A
  non-owned OR missing memo closes with the SAME 1008 code as an auth
  failure — the client can never distinguish "not found" from "not owned"
  from "unauthenticated" (mirrors T-04-IDOR's 404-never-403 discipline).
- T-06-03 (information disclosure — JWT in query string): accepted
  trade-off; browsers cannot set custom headers on a WS handshake (D-08).
- T-06-04 (DoS — long-lived sockets): accepted; the server closes each
  socket on terminal memo status (D-10), bounding connection lifetime to
  one run.
- T-06-04-XCH (cross-memo channel read): the Redis subscription happens
  only AFTER the ownership check passes, so a user can never subscribe to
  another memo's progress channel.
- T-11-04-WSAUTH (spoofing): ``notifications_ws`` reuses
  ``get_current_user_ws`` verbatim; any failure closes 1008 BEFORE
  ``accept()``, so no channel opens for an unauthenticated peer.
- T-11-04-WSCHAN (information disclosure): the subscribed channel is
  derived from the authenticated user's own id (via
  ``app.services.notification_publisher``); the route accepts no resource
  id at all, so a cross-user subscription is not expressible in the
  request.
"""

from __future__ import annotations

import json

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import decode_access_token
from app.db.models import AgentTask, ResearchMemo, ResearchMemoStatus, User
from app.db.session import session_scope
from app.services import notification_publisher
from app.services.auth_service import is_token_revoked
from app.services.notification_service import (
    event_to_payload,
    recent_events_for_user,
    unread_count_for_user,
)
from app.services.progress_publisher import progress_channel

router = APIRouter(prefix="/ws", tags=["ws"])

#: Terminal ResearchMemo statuses — reaching one of these ends the run
#: (D-10); the already-finished / late-join case (D-09) is detected against
#: this set before ever touching pub/sub.
_TERMINAL_MEMO_STATUSES: frozenset[ResearchMemoStatus] = frozenset(
    {
        ResearchMemoStatus.COMPLETE,
        ResearchMemoStatus.PARTIAL,
        ResearchMemoStatus.FAILED,
    }
)


def _new_redis_client(settings: Settings) -> aioredis.Redis:
    """Return a plain (non-``Depends``) Redis client from ``settings.REDIS_URL``.

    Identical call shape to ``app.core.dependencies.get_redis`` /
    ``app.services.progress_publisher._redis`` — this route runs outside
    FastAPI's request-scoped DI (websocket handlers don't resolve
    ``Depends`` the same way as HTTP routes for non-parameter dependencies
    used mid-handler), so it builds its own client the same way.
    """
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def get_current_user_ws(token: str, session: AsyncSession, settings: Settings) -> User | None:
    """WS variant of ``get_current_user`` — returns ``None`` on ANY failure.

    Replicates the identical validation pipeline (decode_access_token →
    require sub/jti → Redis blocklist check via ``is_token_revoked`` → fetch
    User) as ``app.core.dependencies.get_current_user``, but returns
    ``None`` instead of raising ``HTTPException`` — a WS handshake has no
    HTTP response to attach a 401 to, so the caller maps ``None`` to a
    policy-violation close (T-06-01).

    No distinction is made between an invalid token, missing claims, a
    revoked jti, or a missing user — same 401-on-any-failure discipline as
    ``get_current_user`` (T-01-05-02/03).
    """
    try:
        payload = decode_access_token(token, settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM)
    except JWTError:
        return None

    user_id: str | None = payload.get("sub")
    jti: str | None = payload.get("jti")
    if not user_id or not jti:
        return None

    redis = _new_redis_client(settings)
    try:
        if await is_token_revoked(jti, redis):
            return None
    finally:
        await redis.close()

    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def _snapshot_agents(session: AsyncSession, plan_id: object) -> list[dict]:
    """Return the latest AgentTask status per agent_type for *plan_id* (D-09).

    Mirrors ``app/api/v1/research.py``'s reason-query idiom: order by
    ``created_at`` desc and keep only the first (latest) row seen per
    ``agent_type`` — a plan may have prior runs' rows too (D-03 rerun
    lineage).
    """
    result = await session.execute(
        select(AgentTask.agent_type, AgentTask.status)
        .where(AgentTask.plan_id == plan_id)
        .order_by(AgentTask.created_at.desc())
    )
    latest_by_agent_type: dict[str, str] = {}
    for agent_type, status in result.all():
        if agent_type not in latest_by_agent_type:
            latest_by_agent_type[agent_type] = status.value if hasattr(status, "value") else status
    return [
        {"agent_type": agent_type, "status": status}
        for agent_type, status in latest_by_agent_type.items()
    ]


@router.websocket("/research/{memo_id}")
async def research_progress_ws(websocket: WebSocket, memo_id: str) -> None:
    """Stream live per-agent progress for an owned ResearchMemo run.

    Flow (D-08/D-09/D-10):
    1. Authenticate the ``?token=`` query-param JWT (T-06-01) — close(1008)
       BEFORE ``accept()`` on any failure, so no channel opens for an
       unauthenticated peer.
    2. Enforce ownership (T-06-02) — non-owned or missing memo closes with
       the SAME 1008 code, no existence leak.
    3. Accept, subscribe to the memo's Redis progress channel FIRST (to
       avoid a lost-event gap), then send a snapshot of current per-agent
       statuses (D-09).
    4. If the memo is already terminal, send the terminal message and
       close immediately (late-join / already-finished case, D-09).
    5. Otherwise stream live per-agent transitions from pub/sub until the
       terminal memo event arrives, then close (D-10).
    """
    token = websocket.query_params.get("token")
    settings = get_settings()

    async with session_scope() as session:
        user = await get_current_user_ws(token, session, settings) if token else None
        if user is None:
            await websocket.close(code=1008)
            return

        result = await session.execute(
            select(ResearchMemo).where(ResearchMemo.id == memo_id, ResearchMemo.user_id == user.id)
        )
        memo = result.scalar_one_or_none()
        if memo is None:
            await websocket.close(code=1008)
            return

        await websocket.accept()

        redis = _new_redis_client(settings)
        pubsub = redis.pubsub()
        channel = progress_channel(str(memo.id))

        try:
            # Subscribe BEFORE reading the snapshot so no event published
            # between subscribe and snapshot is ever lost.
            await pubsub.subscribe(channel)

            agents = await _snapshot_agents(session, memo.plan_id)
            await websocket.send_json({"type": "snapshot", "agents": agents})

            if memo.status in _TERMINAL_MEMO_STATUSES:
                await websocket.send_json({"type": "terminal", "memo_status": memo.status.value})
            else:
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    data = json.loads(message["data"])
                    if data.get("type") == "agent":
                        await websocket.send_json(
                            {
                                "type": "agent",
                                "agent_type": data["agent_type"],
                                "status": data["status"],
                            }
                        )
                    elif data.get("type") == "memo":
                        await websocket.send_json(
                            {"type": "terminal", "memo_status": data["status"]}
                        )
                        break
        except WebSocketDisconnect:
            # Client left early — clean up without treating this as an error.
            pass
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
            await redis.close()
            try:
                await websocket.close(code=1000)
            except RuntimeError:
                # Already closed (e.g. the client disconnected first).
                pass


@router.websocket("/notifications")
async def notifications_ws(websocket: WebSocket) -> None:
    """Stream live per-user notification pushes for the whole session (WATCH-06, D-09).

    Flow, mirroring ``research_progress_ws`` step for step except where
    noted:
    1. Authenticate the ``?token=`` query-param JWT (T-11-04-WSAUTH) —
       close(1008) BEFORE ``accept()`` on any failure, so no channel opens
       for an unauthenticated peer.
    2. There is NO second ownership check here, and this is the one
       deliberate structural divergence from the memo route: this endpoint
       accepts no resource id at all. The channel is derived from the
       authenticated user's own id, so a cross-user read is not expressible
       in the request (T-11-04-WSCHAN) — this is not a missing check, it is
       an absent attack surface.
    3. Accept, then ``pubsub.subscribe`` to the user's own
       ``notifications:{user_id}`` channel FIRST (to avoid a lost-event
       gap), and only then send the initial state frame — reusing
       ``notification_service``'s helpers is what guarantees this frame and
       the REST ``GET /notifications`` response carry identical rows and an
       identical key set.
    4. Stream every subsequent push from pub/sub indefinitely. Unlike
       ``research_progress_ws``, which the server closes on a terminal memo
       status (D-10, Phase 6), this socket stays open for the life of the
       session because a user may receive many alerts over time (D-09) —
       there is NO terminal event and NO ``break`` in the listen loop; do
       not port the close-on-terminal logic here.
    5. ``except WebSocketDisconnect: pass`` and a ``finally`` block that
       unsubscribes, closes the pubsub, closes the redis client, and closes
       the websocket inside ``try/except RuntimeError`` — byte-for-byte the
       same cleanup shape as ``research_progress_ws``'s ``finally``.
    """
    token = websocket.query_params.get("token")
    settings = get_settings()

    async with session_scope() as session:
        user = await get_current_user_ws(token, session, settings) if token else None
        if user is None:
            await websocket.close(code=1008)
            return

        await websocket.accept()

        redis = _new_redis_client(settings)
        pubsub = redis.pubsub()
        channel = notification_publisher.notification_channel(str(user.id))

        try:
            # Subscribe BEFORE reading the snapshot so no event published
            # between subscribe and snapshot is ever lost.
            await pubsub.subscribe(channel)

            events = await recent_events_for_user(user.id, session)
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "notifications": [event_to_payload(event) for event in events],
                    "unread_count": await unread_count_for_user(user.id, session),
                }
            )

            # No terminal event and no break here (D-09): unlike the
            # per-memo socket above, this one stays open for the whole
            # session, forwarding every notification published on this
            # user's channel until the client disconnects.
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = json.loads(message["data"])
                if data.get("type") == "notification":
                    await websocket.send_json(data)
        except WebSocketDisconnect:
            # Client left early — clean up without treating this as an error.
            pass
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
            await redis.close()
            try:
                await websocket.close(code=1000)
            except RuntimeError:
                # Already closed (e.g. the client disconnected first).
                pass


__all__ = [
    "router",
    "research_progress_ws",
    "notifications_ws",
    "get_current_user_ws",
]
