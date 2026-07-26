"""Tests for the Phase 8 follow-up chat routes (08-03-PLAN.md, CHAT-01..04).

Coverage:
  - GET /research/memo/{memo_id}/chat: empty-state (D-01), ordered history,
    IDOR (other-user -> 404), unknown memo -> 404, unauthenticated -> 401/403.
  - POST /research/memo/{memo_id}/chat: persists user+assistant turns on a
    terminal memo, returns the assistant ChatMessageResponse, history is
    passed to answer_chat_turn on a second call, rejects non-terminal memos
    (400, zero rows, zero service calls), rejects non-owned memos (404, zero
    service calls), rejects unauthenticated requests (401/403), and
    round-trips coverage_exceeded=True on both the persisted row and the
    response.

Reuses the seeding + authed/unauthed client helpers from
tests/api/test_research_api.py and _seed_memo from tests/api/test_memo_routes.py.
``app.api.v1.research.answer_chat_turn`` is patched with an AsyncMock so no
real Groq call happens.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ChatMessage, ResearchMemoStatus, ResearchPlan, User
from tests.api.test_memo_routes import _seed_memo
from tests.api.test_research_api import (
    RESEARCH_URL,
    _make_authed_client,
    _make_unauthed_client,
    _seed_company,
    _seed_research_plan,
    _seed_user,
)


async def _seed_chat_message(
    db_session: AsyncSession,
    memo_id,
    user: User,
    *,
    role: str = "user",
    content: str = "a prior question",
    coverage_exceeded: bool = False,
) -> ChatMessage:
    """Persist a ChatMessage directly and commit in its own transaction, so
    ``created_at`` (Postgres ``now()``, fixed per transaction) differs across
    sequential calls — required for ordering assertions.
    """
    message = ChatMessage(
        memo_id=memo_id,
        user_id=user.id,
        role=role,
        content=content,
        coverage_exceeded=coverage_exceeded,
    )
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)
    return message


# ---------------------------------------------------------------------------
# GET /research/memo/{memo_id}/chat
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_chat_messages_empty_returns_empty_list(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """GET for the owner of a memo with no chat rows returns 200, messages == [] (D-01)."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, user)

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.get(f"{RESEARCH_URL}/memo/{memo.id}/chat")

    assert resp.status_code == 200, resp.text
    assert resp.json()["messages"] == []


@pytest.mark.anyio
async def test_list_chat_messages_returns_ordered_history(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """GET returns rows ordered by created_at ascending with all 5 fields."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, user)

    first = await _seed_chat_message(db_session, memo.id, user, role="user", content="q1")
    second = await _seed_chat_message(
        db_session, memo.id, user, role="assistant", content="a1"
    )
    third = await _seed_chat_message(db_session, memo.id, user, role="user", content="q2")

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.get(f"{RESEARCH_URL}/memo/{memo.id}/chat")

    assert resp.status_code == 200, resp.text
    messages = resp.json()["messages"]
    assert [m["id"] for m in messages] == [
        str(first.id),
        str(second.id),
        str(third.id),
    ]
    for m in messages:
        assert set(m.keys()) == {
            "id",
            "role",
            "content",
            "coverage_exceeded",
            "created_at",
        }


@pytest.mark.anyio
async def test_list_chat_messages_other_user_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A memo owned by a DIFFERENT user returns 404, never 403 (IDOR)."""
    owner = await _seed_user(db_session)
    other_user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, owner, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, owner)

    async with _make_authed_client(db_session, test_settings, other_user) as client:
        resp = await client.get(f"{RESEARCH_URL}/memo/{memo.id}/chat")

    assert resp.status_code == 404, resp.text


@pytest.mark.anyio
async def test_list_chat_messages_unknown_memo_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A random/unknown memo_id returns 404."""
    user = await _seed_user(db_session)
    await db_session.commit()

    random_memo_id = uuid.uuid4()

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.get(f"{RESEARCH_URL}/memo/{random_memo_id}/chat")

    assert resp.status_code == 404, resp.text


@pytest.mark.anyio
async def test_list_chat_messages_requires_auth(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """GET without auth returns 401/403 before any DB work."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, user)

    async with _make_unauthed_client(db_session, test_settings) as client:
        resp = await client.get(f"{RESEARCH_URL}/memo/{memo.id}/chat")

    assert resp.status_code in (401, 403), resp.text


# ---------------------------------------------------------------------------
# POST /research/memo/{memo_id}/chat
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_post_chat_message_success_persists_two_rows(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """POST on a COMPLETE memo returns the assistant message and persists
    exactly 2 new ChatMessage rows (user, then assistant), user.created_at <
    assistant.created_at.
    """
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, user, status=ResearchMemoStatus.COMPLETE)

    mock_answer = AsyncMock(return_value=("The answer is 42.", False))
    async with _make_authed_client(db_session, test_settings, user) as client:
        with patch("app.api.v1.research.answer_chat_turn", new=mock_answer):
            resp = await client.post(
                f"{RESEARCH_URL}/memo/{memo.id}/chat",
                json={"question": "What is the answer?"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "assistant"
    assert body["content"] == "The answer is 42."
    assert body["coverage_exceeded"] is False

    rows_result = await db_session.execute(
        select(ChatMessage).where(ChatMessage.memo_id == memo.id).order_by(
            ChatMessage.created_at.asc()
        )
    )
    rows = rows_result.scalars().all()
    assert len(rows) == 2
    assert rows[0].role == "user"
    assert rows[0].content == "What is the answer?"
    assert rows[1].role == "assistant"
    assert rows[1].content == "The answer is 42."
    assert rows[0].created_at <= rows[1].created_at
    assert rows[0].user_id == user.id
    assert rows[1].user_id == user.id


@pytest.mark.anyio
async def test_post_chat_message_second_call_receives_history(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A second POST loads the prior 2 rows as history and passes them to
    answer_chat_turn (non-empty history on the second call)."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, user, status=ResearchMemoStatus.COMPLETE)

    mock_answer = AsyncMock(return_value=("answer", False))
    async with _make_authed_client(db_session, test_settings, user) as client:
        with patch("app.api.v1.research.answer_chat_turn", new=mock_answer):
            first_resp = await client.post(
                f"{RESEARCH_URL}/memo/{memo.id}/chat",
                json={"question": "first question"},
            )
            assert first_resp.status_code == 200, first_resp.text
            first_history = mock_answer.await_args.args[1]
            assert first_history == []

            second_resp = await client.post(
                f"{RESEARCH_URL}/memo/{memo.id}/chat",
                json={"question": "second question"},
            )
            assert second_resp.status_code == 200, second_resp.text
            second_history = mock_answer.await_args.args[1]
            assert len(second_history) == 2


@pytest.mark.anyio
async def test_post_chat_message_non_terminal_memo_returns_400(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """POST on a PENDING (non-terminal) memo returns 400, creates zero
    ChatMessage rows, and makes zero service calls (D-06)."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, user, status=ResearchMemoStatus.PENDING)

    mock_answer = AsyncMock(return_value=("answer", False))
    async with _make_authed_client(db_session, test_settings, user) as client:
        with patch("app.api.v1.research.answer_chat_turn", new=mock_answer):
            resp = await client.post(
                f"{RESEARCH_URL}/memo/{memo.id}/chat",
                json={"question": "a question"},
            )

    assert resp.status_code == 400, resp.text
    mock_answer.assert_not_awaited()

    rows_result = await db_session.execute(
        select(ChatMessage).where(ChatMessage.memo_id == memo.id)
    )
    assert rows_result.scalars().all() == []


@pytest.mark.anyio
async def test_post_chat_message_other_user_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """POST on a memo owned by a DIFFERENT user returns 404 (IDOR) and makes
    zero service calls."""
    owner = await _seed_user(db_session)
    other_user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, owner, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, owner, status=ResearchMemoStatus.COMPLETE)

    mock_answer = AsyncMock(return_value=("answer", False))
    async with _make_authed_client(db_session, test_settings, other_user) as client:
        with patch("app.api.v1.research.answer_chat_turn", new=mock_answer):
            resp = await client.post(
                f"{RESEARCH_URL}/memo/{memo.id}/chat",
                json={"question": "a question"},
            )

    assert resp.status_code == 404, resp.text
    mock_answer.assert_not_awaited()


@pytest.mark.anyio
async def test_post_chat_message_requires_auth(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """POST unauthenticated returns 401/403 before any DB work."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, user, status=ResearchMemoStatus.COMPLETE)

    mock_answer = AsyncMock(return_value=("answer", False))
    async with _make_unauthed_client(db_session, test_settings) as client:
        with patch("app.api.v1.research.answer_chat_turn", new=mock_answer):
            resp = await client.post(
                f"{RESEARCH_URL}/memo/{memo.id}/chat",
                json={"question": "a question"},
            )

    assert resp.status_code in (401, 403), resp.text
    mock_answer.assert_not_awaited()


@pytest.mark.anyio
async def test_post_chat_message_coverage_exceeded_round_trips(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A coverage_exceeded=True service result is present on both the
    persisted assistant row and the JSON response (CHAT-04)."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, user, status=ResearchMemoStatus.COMPLETE)

    mock_answer = AsyncMock(return_value=("out of scope", True))
    async with _make_authed_client(db_session, test_settings, user) as client:
        with patch("app.api.v1.research.answer_chat_turn", new=mock_answer):
            resp = await client.post(
                f"{RESEARCH_URL}/memo/{memo.id}/chat",
                json={"question": "a question outside the memo"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["coverage_exceeded"] is True

    rows_result = await db_session.execute(
        select(ChatMessage).where(
            ChatMessage.memo_id == memo.id, ChatMessage.role == "assistant"
        )
    )
    assistant_row = rows_result.scalar_one()
    assert assistant_row.coverage_exceeded is True


# ---------------------------------------------------------------------------
# ChatTurnRequest validation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_post_chat_message_rejects_empty_question(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """An empty/whitespace-only question is rejected with 422 before any
    service call."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, user, status=ResearchMemoStatus.COMPLETE)

    mock_answer = AsyncMock(return_value=("answer", False))
    async with _make_authed_client(db_session, test_settings, user) as client:
        with patch("app.api.v1.research.answer_chat_turn", new=mock_answer):
            resp = await client.post(
                f"{RESEARCH_URL}/memo/{memo.id}/chat",
                json={"question": "   "},
            )

    assert resp.status_code == 422, resp.text
    mock_answer.assert_not_awaited()


@pytest.mark.anyio
async def test_post_chat_message_rejects_oversized_question(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A question exceeding _MAX_QUESTION_LENGTH is rejected with 422."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, user, status=ResearchMemoStatus.COMPLETE)

    mock_answer = AsyncMock(return_value=("answer", False))
    async with _make_authed_client(db_session, test_settings, user) as client:
        with patch("app.api.v1.research.answer_chat_turn", new=mock_answer):
            resp = await client.post(
                f"{RESEARCH_URL}/memo/{memo.id}/chat",
                json={"question": "x" * 2001},
            )

    assert resp.status_code == 422, resp.text
    mock_answer.assert_not_awaited()
