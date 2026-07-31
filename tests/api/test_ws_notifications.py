"""Live-notification WebSocket route tests (11-07-PLAN.md, WATCH-06).

Coverage:
- test_no_token_closes_1008 / test_garbage_token_closes_1008 /
  test_revoked_token_closes_1008 / test_unknown_user_closes_1008: four
  distinct auth-failure paths each close 1008 with no frame delivered
  (T-11-04-WSAUTH) — confirms the Redis blocklist check inside
  ``get_current_user_ws`` is not bypassed by the new route.
- test_close_happens_before_accept_on_auth_failure: no snapshot frame is
  ever delivered on any of the failure paths above (the observable
  consequence of closing before ``accept()``).
- test_valid_token_receives_snapshot_first: the first frame is a
  ``{"type": "snapshot", ...}`` frame carrying a notifications list and an
  integer unread_count.
- test_subscribes_to_the_callers_own_channel: ``pubsub.subscribe`` is
  awaited with exactly ``notification_channel(USER_ID)`` — the
  T-11-04-WSCHAN proof: this route accepts no resource id, so the only
  thing preventing a cross-user read is this derivation.
- test_subscribe_precedes_snapshot: subscribe is awaited before the
  snapshot helpers are called (no lost-event gap).
- test_published_notification_is_forwarded: one pushed notification message
  is forwarded to the client with the same payload.
- test_socket_stays_open_after_forwarding_an_event: two pushed messages are
  BOTH received on the same connection (D-09's lifecycle divergence from
  the per-memo socket, which closes after its terminal event; a ported
  ``break`` would make the second frame never arrive).
- test_non_notification_message_is_ignored: a frame whose type is not
  "notification" is never forwarded.
- test_cleanup_unsubscribes_and_closes_redis_on_client_disconnect: after
  the client disconnects, ``pubsub.unsubscribe``, ``pubsub.close``, and
  ``redis.close`` are each awaited.
- test_memo_progress_route_still_works: a smoke assertion that
  ``/api/v1/ws/research/{memo_id}`` is still registered on the composed
  app, guarding against an edit that displaced the Phase 6 route.

No real Postgres or Redis is used — mirrors ``tests/api/test_ws.py``'s
hermetic pattern exactly: ``app.api.v1.ws.session_scope`` and
``app.api.v1.ws._new_redis_client`` are patched with fakes, and
``app.api.v1.ws.recent_events_for_user`` / ``unread_count_for_user`` are
patched with ``AsyncMock``s so the snapshot content is deterministic.
``_TEST_SETTINGS`` and ``_make_token`` are redefined identically to
``tests/api/test_ws.py`` rather than imported, since that module's copies
are private, undocumented-as-shared module internals scoped to memo-progress
concerns; keeping this module self-contained avoids an accidental coupling
between the two test files' unrelated fixtures.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from jose import jwt as jose_jwt
from starlette.websockets import WebSocketDisconnect

from app.api.v1 import ws as ws_module
from app.core.config import Settings
from app.main import create_app
from app.services import notification_publisher

# ---------------------------------------------------------------------------
# Fixed test settings (no real DB/Redis/env required) — mirrors test_ws.py
# ---------------------------------------------------------------------------

_TEST_SETTINGS = Settings(
    DATABASE_URL="postgresql+asyncpg://test:test@localhost:5433/test",
    REDIS_URL="redis://localhost:6379/1",
    JWT_SECRET_KEY="test-jwt-secret-not-for-production",
    JWT_ALGORITHM="HS256",
    JWT_ACCESS_TOKEN_EXPIRE_SECONDS=86400,
    GROQ_API_KEY="test-groq-key-not-for-production",
)

USER_ID = str(uuid.uuid4())


def _ws_url(token: str | None = None) -> str:
    base = "/api/v1/ws/notifications"
    return f"{base}?token={token}" if token is not None else base


def _make_token(*, user_id: str = USER_ID, jti: str | None = None) -> str:
    payload = {
        "sub": user_id,
        "jti": jti or str(uuid.uuid4()),
        "iat": 0,
        "exp": 9999999999,
    }
    return jose_jwt.encode(
        payload, _TEST_SETTINGS.JWT_SECRET_KEY, algorithm=_TEST_SETTINGS.JWT_ALGORITHM
    )


# ---------------------------------------------------------------------------
# Fakes — hermetic stand-ins for Redis + the DB session (no real I/O)
# ---------------------------------------------------------------------------


class _FakeRedisClient:
    """Fake ``redis.asyncio.Redis`` — supports ``.exists()`` (blocklist check
    inside ``get_current_user_ws``) and ``.pubsub()`` (this route's live
    event stream)."""

    def __init__(
        self,
        revoked_jtis: set[str] | None = None,
        events: list[dict] | None = None,
        pubsub: "_FakePubSub | None" = None,
    ):
        self._revoked = revoked_jtis or set()
        self._events = events or []
        self._pubsub = pubsub

    async def exists(self, key: str) -> int:
        jti = key.removeprefix("revoked:")
        return 1 if jti in self._revoked else 0

    async def close(self) -> None:
        return None

    def pubsub(self) -> "_FakePubSub":
        if self._pubsub is not None:
            return self._pubsub
        return _FakePubSub(self._events)


class _FakePubSub:
    """Fake pub/sub handle — ``listen()`` replays a canned event list,
    standing in for messages that would arrive over a real Redis channel."""

    def __init__(self, events: list[dict], call_log: list[str] | None = None):
        self._events = events
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self._call_log = call_log if call_log is not None else []

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)
        self._call_log.append(f"subscribe:{channel}")

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)
        self._call_log.append(f"unsubscribe:{channel}")

    async def close(self) -> None:
        self._call_log.append("pubsub_close")

    async def listen(self):
        for event in self._events:
            yield {"type": "message", "data": _json_dumps(event), "channel": "x", "pattern": None}


def _json_dumps(obj: dict) -> str:
    import json

    return json.dumps(obj)


class _FakeResult:
    """Stand-in for a SQLAlchemy ``Result`` — only ``scalar_one_or_none``,
    the sole accessor this route's ``get_current_user_ws`` uses."""

    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


def _make_fake_user(user_id: str = USER_ID) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    return user


def _make_fake_session(*, user=None):
    """Return an AsyncMock ``AsyncSession`` whose ``.execute()`` yields the
    canned user lookup issued inside ``get_current_user_ws``."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_FakeResult(scalar=user))
    return session


@asynccontextmanager
async def _session_scope_cm(session):
    yield session


def _patched_client(
    *,
    session,
    redis_client,
    recent_events: list[dict] | None = None,
    unread_count: int = 0,
) -> TestClient:
    """Build a TestClient with ``session_scope``/``_new_redis_client`` and
    the two ``notification_service`` snapshot helpers patched at the
    ``app.api.v1.ws`` module level — the route calls these directly rather
    than through FastAPI's ``Depends`` system, so this is the equivalent DI
    seam for WS routes."""
    application = create_app()
    patch("app.api.v1.ws.session_scope", lambda: _session_scope_cm(session)).start()
    patch("app.api.v1.ws.get_settings", return_value=_TEST_SETTINGS).start()
    patch("app.api.v1.ws._new_redis_client", return_value=redis_client).start()
    patch(
        "app.api.v1.ws.recent_events_for_user",
        new=AsyncMock(return_value=recent_events or []),
    ).start()
    patch(
        "app.api.v1.ws.unread_count_for_user",
        new=AsyncMock(return_value=unread_count),
    ).start()
    # recent_events_for_user is mocked to return already-shaped payload
    # dicts directly (not ORM AlertEvent rows), so event_to_payload is
    # patched to the identity function — it would otherwise try to read
    # ``.id``/``.alert_rule_id``/etc. attributes off a plain dict.
    patch("app.api.v1.ws.event_to_payload", new=lambda event: event).start()
    return TestClient(application)


# ---------------------------------------------------------------------------
# Auth-reject tests (T-11-04-WSAUTH)
# ---------------------------------------------------------------------------


def test_no_token_closes_1008():
    """Connecting with NO ``?token=`` closes 1008 before any snapshot."""
    session = _make_fake_session()
    redis_client = _FakeRedisClient()
    client = _patched_client(session=session, redis_client=redis_client)

    try:
        with client.websocket_connect(_ws_url(token=None)):
            pass
        raise AssertionError("expected WebSocketDisconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008

    session.execute.assert_not_called()
    patch.stopall()


def test_garbage_token_closes_1008():
    """An undecodable/garbage token closes 1008 with no snapshot delivered."""
    session = _make_fake_session()
    redis_client = _FakeRedisClient()
    client = _patched_client(session=session, redis_client=redis_client)

    try:
        with client.websocket_connect(_ws_url(token="not-a-real-jwt")):
            pass
        raise AssertionError("expected WebSocketDisconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008

    session.execute.assert_not_called()
    patch.stopall()


def test_revoked_token_closes_1008():
    """A syntactically valid but blocklisted jti closes 1008 — confirms the
    Redis blocklist check inside ``get_current_user_ws`` is not bypassed by
    this route."""
    jti = str(uuid.uuid4())
    token = _make_token(jti=jti)
    session = _make_fake_session()
    redis_client = _FakeRedisClient(revoked_jtis={jti})
    client = _patched_client(session=session, redis_client=redis_client)

    try:
        with client.websocket_connect(_ws_url(token=token)):
            pass
        raise AssertionError("expected WebSocketDisconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008

    session.execute.assert_not_called()
    patch.stopall()


def test_unknown_user_closes_1008():
    """A well-formed token whose ``sub`` matches no user row closes 1008."""
    token = _make_token(user_id=str(uuid.uuid4()))
    session = _make_fake_session(user=None)
    redis_client = _FakeRedisClient()
    client = _patched_client(session=session, redis_client=redis_client)

    try:
        with client.websocket_connect(_ws_url(token=token)):
            pass
        raise AssertionError("expected WebSocketDisconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008

    patch.stopall()


def test_close_happens_before_accept_on_auth_failure():
    """No snapshot frame is ever delivered on any auth-failure path — the
    observable consequence of closing before ``accept()``."""
    for build_client_kwargs in (
        {"session": _make_fake_session(), "redis_client": _FakeRedisClient()},
        {"session": _make_fake_session(user=None), "redis_client": _FakeRedisClient()},
    ):
        client = _patched_client(**build_client_kwargs)
        try:
            with client.websocket_connect(_ws_url(token=None)) as ws:
                with pytest.raises(Exception):
                    ws.receive_json()
        except WebSocketDisconnect as exc:
            assert exc.code == 1008
        patch.stopall()


# ---------------------------------------------------------------------------
# Snapshot tests (T-11-04-WSCHAN, D-09)
# ---------------------------------------------------------------------------


def test_valid_token_receives_snapshot_first():
    """The first frame is ``{"type": "snapshot", ...}`` carrying a
    notifications list and an integer unread_count."""
    token = _make_token(user_id=USER_ID)
    user = _make_fake_user(user_id=USER_ID)
    session = _make_fake_session(user=user)
    redis_client = _FakeRedisClient()
    recent = [
        {
            "id": str(uuid.uuid4()),
            "alert_rule_id": str(uuid.uuid4()),
            "message": "hi",
            "triggered_at": "2026-01-01T00:00:00+00:00",
            "read": False,
        }
    ]
    client = _patched_client(
        session=session, redis_client=redis_client, recent_events=recent, unread_count=3
    )

    with client.websocket_connect(_ws_url(token=token)) as ws:
        snapshot = ws.receive_json()

    assert snapshot["type"] == "snapshot"
    assert snapshot["notifications"] == recent
    assert isinstance(snapshot["unread_count"], int)
    assert snapshot["unread_count"] == 3

    patch.stopall()


def test_subscribes_to_the_callers_own_channel():
    """``pubsub.subscribe`` is awaited with exactly
    ``notification_channel(USER_ID)`` — the T-11-04-WSCHAN proof: the route
    accepts no resource id, so the only thing preventing a cross-user read
    is this derivation."""
    token = _make_token(user_id=USER_ID)
    user = _make_fake_user(user_id=USER_ID)
    session = _make_fake_session(user=user)
    pubsub = _FakePubSub(events=[])
    redis_client = _FakeRedisClient(pubsub=pubsub)
    client = _patched_client(session=session, redis_client=redis_client)

    with client.websocket_connect(_ws_url(token=token)) as ws:
        ws.receive_json()  # snapshot

    assert pubsub.subscribed == [notification_publisher.notification_channel(USER_ID)]

    patch.stopall()


def test_subscribe_precedes_snapshot():
    """``subscribe`` is awaited before the snapshot helpers are called (no
    lost-event gap)."""
    token = _make_token(user_id=USER_ID)
    user = _make_fake_user(user_id=USER_ID)
    session = _make_fake_session(user=user)
    call_log: list[str] = []
    pubsub = _FakePubSub(events=[], call_log=call_log)
    redis_client = _FakeRedisClient(pubsub=pubsub)

    application = create_app()
    patch("app.api.v1.ws.session_scope", lambda: _session_scope_cm(session)).start()
    patch("app.api.v1.ws.get_settings", return_value=_TEST_SETTINGS).start()
    patch("app.api.v1.ws._new_redis_client", return_value=redis_client).start()

    async def _recent_events_for_user(*args, **kwargs):
        call_log.append("recent_events_for_user")
        return []

    async def _unread_count_for_user(*args, **kwargs):
        call_log.append("unread_count_for_user")
        return 0

    patch(
        "app.api.v1.ws.recent_events_for_user", new=AsyncMock(side_effect=_recent_events_for_user)
    ).start()
    patch(
        "app.api.v1.ws.unread_count_for_user", new=AsyncMock(side_effect=_unread_count_for_user)
    ).start()
    client = TestClient(application)

    with client.websocket_connect(_ws_url(token=token)) as ws:
        ws.receive_json()  # snapshot

    subscribe_index = next(i for i, c in enumerate(call_log) if c.startswith("subscribe:"))
    snapshot_helper_indices = [
        i for i, c in enumerate(call_log) if c in ("recent_events_for_user", "unread_count_for_user")
    ]
    assert snapshot_helper_indices, "snapshot helpers were never called"
    assert subscribe_index < min(snapshot_helper_indices)

    patch.stopall()


# ---------------------------------------------------------------------------
# Forwarding + lifecycle tests (D-09)
# ---------------------------------------------------------------------------


def test_published_notification_is_forwarded():
    """One pushed ``{"type": "notification", ...}`` message is forwarded to
    the client with the same payload."""
    token = _make_token(user_id=USER_ID)
    user = _make_fake_user(user_id=USER_ID)
    session = _make_fake_session(user=user)
    notification = {
        "type": "notification",
        "id": str(uuid.uuid4()),
        "alert_rule_id": str(uuid.uuid4()),
        "message": "AAPL filed a new 10-K",
        "triggered_at": "2026-01-01T00:00:00+00:00",
        "read": False,
    }
    redis_client = _FakeRedisClient(events=[notification])
    client = _patched_client(session=session, redis_client=redis_client)

    with client.websocket_connect(_ws_url(token=token)) as ws:
        ws.receive_json()  # snapshot
        forwarded = ws.receive_json()

    assert forwarded == notification

    patch.stopall()


def test_socket_stays_open_after_forwarding_an_event():
    """Two pushed notification messages are BOTH received on the same
    connection — the D-09 lifecycle divergence from the per-memo socket,
    which closes after its terminal event; a ported ``break`` would make
    the second frame never arrive."""
    token = _make_token(user_id=USER_ID)
    user = _make_fake_user(user_id=USER_ID)
    session = _make_fake_session(user=user)
    first = {
        "type": "notification",
        "id": str(uuid.uuid4()),
        "alert_rule_id": str(uuid.uuid4()),
        "message": "first",
        "triggered_at": "2026-01-01T00:00:00+00:00",
        "read": False,
    }
    second = {
        "type": "notification",
        "id": str(uuid.uuid4()),
        "alert_rule_id": str(uuid.uuid4()),
        "message": "second",
        "triggered_at": "2026-01-01T00:01:00+00:00",
        "read": False,
    }
    redis_client = _FakeRedisClient(events=[first, second])
    client = _patched_client(session=session, redis_client=redis_client)

    with client.websocket_connect(_ws_url(token=token)) as ws:
        ws.receive_json()  # snapshot
        forwarded_first = ws.receive_json()
        forwarded_second = ws.receive_json()

    assert forwarded_first == first
    assert forwarded_second == second

    patch.stopall()


def test_non_notification_message_is_ignored():
    """A frame whose type is not "notification" is never forwarded — only
    the real notification arrives."""
    token = _make_token(user_id=USER_ID)
    user = _make_fake_user(user_id=USER_ID)
    session = _make_fake_session(user=user)
    ignored = {"type": "heartbeat", "at": "2026-01-01T00:00:00+00:00"}
    real = {
        "type": "notification",
        "id": str(uuid.uuid4()),
        "alert_rule_id": str(uuid.uuid4()),
        "message": "real one",
        "triggered_at": "2026-01-01T00:00:00+00:00",
        "read": False,
    }
    redis_client = _FakeRedisClient(events=[ignored, real])
    client = _patched_client(session=session, redis_client=redis_client)

    with client.websocket_connect(_ws_url(token=token)) as ws:
        ws.receive_json()  # snapshot
        forwarded = ws.receive_json()

    assert forwarded == real

    patch.stopall()


def test_cleanup_unsubscribes_and_closes_redis_on_client_disconnect():
    """After the client closes, ``pubsub.unsubscribe``, ``pubsub.close``,
    and ``redis.close`` were each awaited."""
    token = _make_token(user_id=USER_ID)
    user = _make_fake_user(user_id=USER_ID)
    session = _make_fake_session(user=user)
    pubsub = _FakePubSub(events=[])
    redis_client = _FakeRedisClient(pubsub=pubsub)
    redis_client.close = AsyncMock()
    client = _patched_client(session=session, redis_client=redis_client)

    with client.websocket_connect(_ws_url(token=token)) as ws:
        ws.receive_json()  # snapshot

    assert pubsub.unsubscribed == [notification_publisher.notification_channel(USER_ID)]
    redis_client.close.assert_awaited()

    patch.stopall()


# ---------------------------------------------------------------------------
# Phase 6 regression smoke test
# ---------------------------------------------------------------------------


def test_memo_progress_route_still_works():
    """``/api/v1/ws/research/{memo_id}`` is still registered on the composed
    app — guards against an edit that displaced the Phase 6 route."""
    application = create_app()
    ws_paths = {
        route.path
        for route in application.routes
        if getattr(route, "path", None) and "/ws/" in route.path
    }
    assert "/api/v1/ws/research/{memo_id}" in ws_paths
    assert "/api/v1/ws/notifications" in ws_paths
    assert hasattr(ws_module, "research_progress_ws")
