"""Live-progress WebSocket route tests (06-04-PLAN.md, EXEC-01).

Coverage:
- test_no_token_closes_1008: connecting with NO token closes 1008, no
  snapshot delivered (T-06-01).
- test_garbage_token_closes_1008: connecting with an undecodable token
  closes 1008 (T-06-01).
- test_revoked_token_closes_1008: a syntactically valid but blocklisted
  jti closes 1008 (T-06-01, confirms the blocklist check is not skipped).
- test_other_user_memo_closes_1008: an owned-by-someone-else (or missing)
  memo_id closes 1008 — indistinguishable from the auth-failure case
  (T-06-02, no existence leak).
- test_owned_memo_already_terminal_sends_snapshot_then_terminal: a
  late-join / already-finished (PARTIAL) run delivers snapshot -> terminal
  -> clean close (D-09).
- test_live_agent_event_forwarded_then_terminal_closes: a running memo
  streams a forwarded agent event, then the terminal memo event, then the
  server closes the socket itself (D-10).
- test_get_current_user_ws_checks_blocklist: source-level guard confirming
  ``is_token_revoked`` is called inside ``get_current_user_ws`` (mirrors
  the plan's ``grep -c "is_token_revoked" app/api/v1/ws.py`` acceptance
  check).

No real Postgres or Redis is used — ``app.api.v1.ws.session_scope`` and
``app.api.v1.ws._new_redis_client`` are patched with hermetic fakes
(mirrors ``tests/test_ingest_api.py``'s TestClient + dependency-override
conventions; this route calls ``session_scope()``/``get_settings()``
directly rather than through FastAPI's ``Depends`` system, so patching the
module-level names in ``app.api.v1.ws`` is the equivalent seam).
"""

from __future__ import annotations

import inspect
import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from jose import jwt as jose_jwt
from starlette.websockets import WebSocketDisconnect

from app.api.v1 import ws as ws_module
from app.core.config import Settings
from app.db.models import AgentTaskStatus, ResearchMemoStatus
from app.main import create_app

# ---------------------------------------------------------------------------
# Fixed test settings (no real DB/Redis/env required)
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
OTHER_USER_ID = str(uuid.uuid4())
MEMO_ID = str(uuid.uuid4())
PLAN_ID = str(uuid.uuid4())


def _ws_url(memo_id: str = MEMO_ID, token: str | None = None) -> str:
    base = f"/api/v1/ws/research/{memo_id}"
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
    inside ``get_current_user_ws``) and ``.pubsub()`` (the route's live
    event stream)."""

    def __init__(self, revoked_jtis: set[str] | None = None, events: list[dict] | None = None):
        self._revoked = revoked_jtis or set()
        self._events = events or []

    async def exists(self, key: str) -> int:
        jti = key.removeprefix("revoked:")
        return 1 if jti in self._revoked else 0

    async def close(self) -> None:
        return None

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self._events)


class _FakePubSub:
    """Fake pub/sub handle — ``listen()`` replays a canned event list,
    standing in for messages that would arrive over a real Redis channel."""

    def __init__(self, events: list[dict]):
        self._events = events
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)

    async def close(self) -> None:
        return None

    async def listen(self):
        for event in self._events:
            yield {"type": "message", "data": json.dumps(event), "channel": "x", "pattern": None}


class _FakeResult:
    """Stand-in for a SQLAlchemy ``Result`` — only the two accessor methods
    the route uses (``scalar_one_or_none``, ``all``)."""

    def __init__(self, scalar=None, rows: list[tuple] | None = None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows


def _make_fake_user(user_id: str = USER_ID) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    return user


def _make_fake_memo(
    *,
    memo_id: str = MEMO_ID,
    user_id: str = USER_ID,
    plan_id: str = PLAN_ID,
    status: ResearchMemoStatus = ResearchMemoStatus.RUNNING,
) -> MagicMock:
    memo = MagicMock()
    memo.id = memo_id
    memo.user_id = user_id
    memo.plan_id = plan_id
    memo.status = status
    return memo


def _make_fake_session(*, user=None, memo=None, agent_rows: list[tuple] | None = None):
    """Return an AsyncMock ``AsyncSession`` whose ``.execute()`` yields canned
    results in the FIXED order the route issues them:
      1. ``select(User)...``            (auth, inside get_current_user_ws)
      2. ``select(ResearchMemo)...``     (ownership check)
      3. ``select(AgentTask...)...``     (snapshot; only reached if owned)
    """
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _FakeResult(scalar=user),
            _FakeResult(scalar=memo),
            _FakeResult(rows=agent_rows or []),
        ]
    )
    return session


@asynccontextmanager
async def _session_scope_cm(session):
    yield session


def _patched_client(*, session, redis_client) -> TestClient:
    """Build a TestClient with ``session_scope``/``get_settings``/
    ``_new_redis_client`` patched at the ``app.api.v1.ws`` module level —
    the route calls these directly rather than through FastAPI's
    ``Depends`` system, so this is the equivalent DI seam for WS routes.
    """
    application = create_app()
    patch("app.api.v1.ws.session_scope", lambda: _session_scope_cm(session)).start()
    patch("app.api.v1.ws.get_settings", return_value=_TEST_SETTINGS).start()
    patch("app.api.v1.ws._new_redis_client", return_value=redis_client).start()
    return TestClient(application)


# ---------------------------------------------------------------------------
# Auth-reject tests (T-06-01)
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

    # No DB reads should have happened — the route short-circuits before
    # ever calling get_current_user_ws.
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
    """A syntactically valid but blocklisted jti closes 1008 (T-06-01) —
    confirms the Redis blocklist check is not skipped."""
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

    # Blocklisted before any user-fetch query.
    session.execute.assert_not_called()
    patch.stopall()


# ---------------------------------------------------------------------------
# Ownership-reject test (T-06-02 — no existence leak)
# ---------------------------------------------------------------------------


def test_other_user_memo_closes_1008():
    """A memo not owned by the authenticated user closes with the SAME 1008
    code as an auth failure — indistinguishable from "not found"."""
    token = _make_token(user_id=USER_ID)
    user = _make_fake_user(user_id=USER_ID)
    # Ownership query filters on user_id == authenticated user's id; a memo
    # owned by someone else never matches, so the mocked query returns None.
    session = _make_fake_session(user=user, memo=None)
    redis_client = _FakeRedisClient()
    client = _patched_client(session=session, redis_client=redis_client)

    try:
        with client.websocket_connect(_ws_url(memo_id=MEMO_ID, token=token)):
            pass
        raise AssertionError("expected WebSocketDisconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008

    patch.stopall()


# ---------------------------------------------------------------------------
# Snapshot -> terminal -> close tests (D-09, D-10)
# ---------------------------------------------------------------------------


def test_owned_memo_already_terminal_sends_snapshot_then_terminal():
    """A late-join on an already-PARTIAL run delivers snapshot -> terminal
    -> clean close, without ever entering the pub/sub loop."""
    token = _make_token(user_id=USER_ID)
    user = _make_fake_user(user_id=USER_ID)
    memo = _make_fake_memo(status=ResearchMemoStatus.PARTIAL)
    agent_rows = [
        ("FundamentalAnalysis", AgentTaskStatus.SUCCESS),
        ("SentimentNLP", AgentTaskStatus.FAILED),
    ]
    session = _make_fake_session(user=user, memo=memo, agent_rows=agent_rows)
    redis_client = _FakeRedisClient()
    client = _patched_client(session=session, redis_client=redis_client)

    with client.websocket_connect(_ws_url(token=token)) as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert {"agent_type": "FundamentalAnalysis", "status": "SUCCESS"} in snapshot["agents"]
        assert {"agent_type": "SentimentNLP", "status": "FAILED"} in snapshot["agents"]

        terminal = ws.receive_json()
        assert terminal == {"type": "terminal", "memo_status": "PARTIAL"}

    patch.stopall()


def test_live_agent_event_forwarded_then_terminal_closes():
    """A RUNNING memo streams a forwarded agent event, then the terminal
    memo event, and the server closes the socket itself (D-10)."""
    token = _make_token(user_id=USER_ID)
    user = _make_fake_user(user_id=USER_ID)
    memo = _make_fake_memo(status=ResearchMemoStatus.RUNNING)
    session = _make_fake_session(user=user, memo=memo, agent_rows=[])
    events = [
        {"type": "agent", "agent_type": "FundamentalAnalysis", "status": "RUNNING"},
        {"type": "memo", "status": "COMPLETE"},
    ]
    redis_client = _FakeRedisClient(events=events)
    client = _patched_client(session=session, redis_client=redis_client)

    try:
        with client.websocket_connect(_ws_url(token=token)) as ws:
            snapshot = ws.receive_json()
            assert snapshot == {"type": "snapshot", "agents": []}

            agent_event = ws.receive_json()
            assert agent_event == {
                "type": "agent",
                "agent_type": "FundamentalAnalysis",
                "status": "RUNNING",
            }

            terminal = ws.receive_json()
            assert terminal == {"type": "terminal", "memo_status": "COMPLETE"}

            # The server closes its own end after the terminal event (D-10);
            # a subsequent receive surfaces that close.
            ws.receive()
    except WebSocketDisconnect as exc:
        assert exc.code == 1000

    patch.stopall()


# ---------------------------------------------------------------------------
# Source-level guard (mirrors the plan's grep-based acceptance check)
# ---------------------------------------------------------------------------


def test_get_current_user_ws_checks_blocklist():
    """``get_current_user_ws`` must call ``is_token_revoked`` before ever
    returning a user — the blocklist check is never skipped (T-06-01)."""
    source = inspect.getsource(ws_module.get_current_user_ws)
    assert "is_token_revoked" in source
