"""Tests for app.workers.tasks._run_research_async (EXEC-05, 06-03-PLAN.md).

Coverage:
- test_run_research_async_updates_existing_memo_with_full_body: mocks
  build_research_graph to return a fixed final_state and patches
  publish_memo_terminal + session_scope to a real test-postgres session;
  asserts the existing memo row is updated to the mapped terminal status
  with a full six-section body (EXEC-04 reason present for the failed
  section), publish_memo_terminal awaited once with the final status, and
  no second ResearchMemo row is created for the plan.

Uses the ``db_session``/``test_settings`` fixtures from ``tests/conftest.py``
(real test-postgres on port 5433, skips automatically when unreachable, per
D-03). ``build_research_graph`` and ``publish_memo_terminal`` are patched at
the ``app.workers.tasks`` import site (never touching a real broker/Redis/
Groq call); ``session_scope`` is patched to yield the real ``db_session`` so
persistence assertions run against a real DB row, mirroring
``tests/api/test_research_api.py``'s db_session-backed pattern.
"""

import contextlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AgentOutput,
    AgentOutputCompleteness,
    AgentTask,
    AgentTaskStatus,
    Company,
    ResearchMemo,
    ResearchMemoStatus,
    ResearchPlan,
    ResearchRequest,
    User,
)
from app.workers.tasks import _run_research_async

pytestmark = pytest.mark.anyio


async def _seed_user(db_session: AsyncSession) -> User:
    user = User(
        email=f"{uuid.uuid4()}@example.com",
        password_hash="not-a-real-hash",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


async def _seed_company(
    db_session: AsyncSession, ticker: str = "AAPL", name: str = "Apple Inc."
) -> Company:
    company = Company(ticker=ticker, name=name)
    db_session.add(company)
    await db_session.flush()
    return company


async def _seed_plan(db_session: AsyncSession, owner: User) -> ResearchPlan:
    request = ResearchRequest(
        user_id=owner.id, raw_query="Tell me about Apple", status="RESOLVED"
    )
    db_session.add(request)
    await db_session.flush()

    plan = ResearchPlan(
        request_id=request.id, user_id=owner.id, resolved_tickers=["AAPL"]
    )
    db_session.add(plan)
    await db_session.flush()
    await db_session.refresh(plan)
    return plan


async def _seed_pending_memo(
    db_session: AsyncSession, plan: ResearchPlan, owner: User
) -> ResearchMemo:
    memo = ResearchMemo(
        plan_id=plan.id,
        user_id=owner.id,
        ticker="AAPL",
        status=ResearchMemoStatus.PENDING,
        body=None,
    )
    db_session.add(memo)
    await db_session.flush()
    await db_session.refresh(memo)
    return memo


async def _seed_failed_sentiment_output(
    db_session: AsyncSession, plan: ResearchPlan
) -> None:
    """Seed an AgentTask/AgentOutput pair so the EXEC-04 reason lookup finds
    a non-null reason for the FAILED SentimentNLP section."""
    task = AgentTask(
        plan_id=plan.id,
        agent_type="SentimentNLP",
        status=AgentTaskStatus.FAILED,
        input={},
    )
    db_session.add(task)
    await db_session.flush()

    output = AgentOutput(
        task_id=task.id,
        completeness=AgentOutputCompleteness.PARTIAL,
        missing_fields="NewsAPI request timed out",
        output={},
    )
    db_session.add(output)
    await db_session.flush()


_FINAL_STATE = {
    "fundamentals_output": {"narrative": "Strong revenue growth."},
    "fundamentals_status": "SUCCESS",
    "sentiment_output": None,
    "sentiment_status": "FAILED",
    "risk_output": {"narrative": "Moderate risk."},
    "risk_status": "SUCCESS",
    "macro_output": {"narrative": "Stable macro backdrop."},
    "macro_status": "SUCCESS",
    "comparables_output": {"narrative": "Trades in line with peers."},
    "comparables_status": "SUCCESS",
    "synthesis_output": {
        "narrative": "Overall positive outlook.",
        "contradictions": [],
    },
    "synthesis_status": "SUCCESS",
    "memo_status": "PARTIAL",
}


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


async def test_run_research_async_updates_existing_memo_with_full_body(
    db_session: AsyncSession,
):
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    await _seed_failed_sentiment_output(db_session, plan)
    await db_session.commit()

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value=_FINAL_STATE)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db_session

    with (
        patch("app.workers.tasks.build_research_graph", return_value=mock_graph),
        patch(
            "app.workers.tasks.publish_memo_terminal", new=AsyncMock()
        ) as mock_publish_terminal,
        patch("app.workers.tasks.session_scope", _fake_session_scope),
    ):
        await _run_research_async(
            memo_id=str(memo.id),
            plan_id=str(plan.id),
            ticker="AAPL",
            user_id=str(owner.id),
        )

    await db_session.refresh(memo)

    assert memo.status == ResearchMemoStatus.PARTIAL
    assert memo.body is not None
    assert set(memo.body.keys()) == {
        "fundamentals",
        "sentiment",
        "risks",
        "macro",
        "comparables",
        "synthesis",
    }
    # SUCCESS sections store the output as-is.
    assert memo.body["fundamentals"] == {"narrative": "Strong revenue growth."}
    # FAILED section is never dropped (EXEC-04) and carries a non-null reason.
    assert memo.body["sentiment"]["narrative"] is None
    assert memo.body["sentiment"]["status"] == "FAILED"
    assert memo.body["sentiment"]["reason"] == "NewsAPI request timed out"

    mock_publish_terminal.assert_awaited_once_with(str(memo.id), "PARTIAL")

    # No second ResearchMemo row was created for the plan.
    count_result = await db_session.execute(
        select(func.count()).select_from(ResearchMemo).where(
            ResearchMemo.plan_id == plan.id
        )
    )
    assert count_result.scalar_one() == 1


async def test_run_research_async_carries_contradictions_through(
    db_session: AsyncSession,
):
    """MEMO-04: contradictions produced by Synthesis (Plan 02) flow
    unmodified through body assembly into ResearchMemo.body."""
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    await db_session.commit()

    contradictions = [
        {
            "topic": "Revenue growth outlook",
            "agents": ["FundamentalAnalysis", "SentimentNLP"],
            "description": "Fundamentals shows accelerating growth while "
            "sentiment coverage is broadly negative.",
            "severity": "medium",
        }
    ]
    final_state = {
        **_FINAL_STATE,
        "synthesis_output": {
            "narrative": "Overall positive outlook.",
            "contradictions": contradictions,
        },
    }

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value=final_state)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db_session

    with (
        patch("app.workers.tasks.build_research_graph", return_value=mock_graph),
        patch("app.workers.tasks.publish_memo_terminal", new=AsyncMock()),
        patch("app.workers.tasks.session_scope", _fake_session_scope),
    ):
        await _run_research_async(
            memo_id=str(memo.id),
            plan_id=str(plan.id),
            ticker="AAPL",
            user_id=str(owner.id),
        )

    await db_session.refresh(memo)

    assert memo.body["synthesis"]["contradictions"] == contradictions


async def test_run_research_async_synthesis_failed_still_has_empty_contradictions(
    db_session: AsyncSession,
):
    """EXEC-04 precedent applied to MEMO-04: a FAILED synthesis section
    still exposes an empty contradictions list, never a missing key."""
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    await db_session.commit()

    final_state = {
        **_FINAL_STATE,
        "synthesis_output": None,
        "synthesis_status": "FAILED",
        "memo_status": "FAILED",
    }

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value=final_state)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db_session

    with (
        patch("app.workers.tasks.build_research_graph", return_value=mock_graph),
        patch("app.workers.tasks.publish_memo_terminal", new=AsyncMock()),
        patch("app.workers.tasks.session_scope", _fake_session_scope),
    ):
        await _run_research_async(
            memo_id=str(memo.id),
            plan_id=str(plan.id),
            ticker="AAPL",
            user_id=str(owner.id),
        )

    await db_session.refresh(memo)

    assert memo.body["synthesis"]["contradictions"] == []


async def test_run_research_async_marks_memo_failed_on_unexpected_exception(
    db_session: AsyncSession,
):
    """Belt-and-suspenders guard: an unexpected exception during graph
    invocation/body assembly forces the memo to FAILED and still publishes
    a terminal event, so the memo never hangs in RUNNING."""
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    await db_session.commit()

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db_session

    with (
        patch("app.workers.tasks.build_research_graph", return_value=mock_graph),
        patch(
            "app.workers.tasks.publish_memo_terminal", new=AsyncMock()
        ) as mock_publish_terminal,
        patch("app.workers.tasks.session_scope", _fake_session_scope),
    ):
        await _run_research_async(
            memo_id=str(memo.id),
            plan_id=str(plan.id),
            ticker="AAPL",
            user_id=str(owner.id),
        )

    await db_session.refresh(memo)
    assert memo.status == ResearchMemoStatus.FAILED
    mock_publish_terminal.assert_awaited_once_with(str(memo.id), "FAILED")
