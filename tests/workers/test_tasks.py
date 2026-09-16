"""Tests for app.workers.tasks._run_research_async (EXEC-05, 06-03-PLAN.md).

Coverage:
- test_run_research_async_updates_existing_memo_with_full_body: mocks
  build_research_graph to return a fixed final_state and patches
  publish_memo_terminal + session_scope to a real test-postgres session;
  asserts the existing memo row is updated to the mapped terminal status
  with a full six-section body (EXEC-04 reason present for the failed
  section), publish_memo_terminal awaited once with the final status, and
  no second ResearchMemo row is created for the plan.
- test_run_research_async_sets_ambient_plan_id_for_graph_invocation /
  test_run_research_async_ambient_plan_id_is_plan_id_not_memo_id (12-13
  Task 1): the run's plan id is visible as the ambient plan id inside the
  graph invocation, and is the plan_id argument, never the memo_id.
- MEMO-06/OBS-02 cost aggregation (12-13 Task 2): body["cost"]["tokens"]
  sums this run's own AgentOutput prompt/completion token columns (latest
  AgentTask per agent_type only, so a rerun's older rows never double-
  count); body["cost"]["api_calls"] equals the patched counter read,
  awaited exactly once with the run's plan id; NULL token columns
  contribute zero without raising; the cost key is present with a zero
  token total even when every section failed; and the cost value carries
  exactly the two keys ``tokens``/``api_calls``, never a monetary figure.

Uses the ``db_session``/``test_settings`` fixtures from ``tests/conftest.py``
(real test-postgres on port 5433, skips automatically when unreachable, per
D-03). ``build_research_graph`` and ``publish_memo_terminal`` are patched at
the ``app.workers.tasks`` import site (never touching a real broker/Redis/
Groq call); ``session_scope`` is patched to yield the real ``db_session`` so
persistence assertions run against a real DB row, mirroring
``tests/api/test_research_api.py``'s db_session-backed pattern.
``read_and_clear_api_call_count`` is patched with an ``AsyncMock`` in every
test that runs ``_run_research_async`` (including tests predating the cost
aggregation) so no test depends on a real Redis connection.
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
from app.services.api_call_counter import get_current_plan_id
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
    request = ResearchRequest(user_id=owner.id, raw_query="Tell me about Apple", status="RESOLVED")
    db_session.add(request)
    await db_session.flush()

    plan = ResearchPlan(request_id=request.id, user_id=owner.id, resolved_tickers=["AAPL"])
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


async def _seed_failed_sentiment_output(db_session: AsyncSession, plan: ResearchPlan) -> None:
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


async def _seed_agent_output_with_tokens(
    db_session: AsyncSession,
    plan: ResearchPlan,
    agent_type: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> None:
    """Seed an AgentTask/AgentOutput pair carrying the given token counts
    for ``agent_type`` (12-13 Task 2 cost-aggregation coverage). Either
    token argument may be ``None`` to model a degraded agent row whose
    Groq call never completed."""
    task = AgentTask(
        plan_id=plan.id,
        agent_type=agent_type,
        status=AgentTaskStatus.SUCCESS,
        input={},
    )
    db_session.add(task)
    await db_session.flush()

    output = AgentOutput(
        task_id=task.id,
        completeness=AgentOutputCompleteness.FULL,
        missing_fields=None,
        output={},
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
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
            "app.workers.tasks.read_and_clear_api_call_count",
            new=AsyncMock(return_value=0),
        ),
        patch("app.workers.tasks.publish_memo_terminal", new=AsyncMock()) as mock_publish_terminal,
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
        "cost",
    }
    # MEMO-06/OBS-02: the cost key is always present (Phase 7 body-assembly
    # guarantee), zero tokens here since no AgentOutput rows were seeded.
    assert memo.body["cost"] == {"tokens": 0, "api_calls": 0}
    # SUCCESS sections store the output as-is, except METRIC-02's
    # anomalies backfill unconditionally guarantees the key is present.
    assert memo.body["fundamentals"] == {
        "narrative": "Strong revenue growth.",
        "anomalies": [],
    }
    # FAILED section is never dropped (EXEC-04) and carries a non-null reason.
    assert memo.body["sentiment"]["narrative"] is None
    assert memo.body["sentiment"]["status"] == "FAILED"
    assert memo.body["sentiment"]["reason"] == "NewsAPI request timed out"

    mock_publish_terminal.assert_awaited_once_with(str(memo.id), "PARTIAL")

    # No second ResearchMemo row was created for the plan.
    count_result = await db_session.execute(
        select(func.count()).select_from(ResearchMemo).where(ResearchMemo.plan_id == plan.id)
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
        patch(
            "app.workers.tasks.read_and_clear_api_call_count",
            new=AsyncMock(return_value=0),
        ),
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
        patch(
            "app.workers.tasks.read_and_clear_api_call_count",
            new=AsyncMock(return_value=0),
        ),
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


async def test_run_research_async_fundamentals_failed_still_has_empty_anomalies(
    db_session: AsyncSession,
):
    """METRIC-02 precedent applied to EXEC-04: a FAILED fundamentals section
    still exposes an empty anomalies list, never a missing key."""
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    await db_session.commit()

    final_state = {
        **_FINAL_STATE,
        "fundamentals_output": None,
        "fundamentals_status": "FAILED",
        "memo_status": "PARTIAL",
    }

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value=final_state)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db_session

    with (
        patch("app.workers.tasks.build_research_graph", return_value=mock_graph),
        patch(
            "app.workers.tasks.read_and_clear_api_call_count",
            new=AsyncMock(return_value=0),
        ),
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

    assert memo.body["fundamentals"]["anomalies"] == []


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
            "app.workers.tasks.read_and_clear_api_call_count",
            new=AsyncMock(return_value=0),
        ),
        patch("app.workers.tasks.publish_memo_terminal", new=AsyncMock()) as mock_publish_terminal,
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


async def test_run_research_async_sets_ambient_plan_id_for_graph_invocation(
    db_session: AsyncSession,
):
    """D-05/OBS-02: the run's plan id is visible as the ambient plan id to
    code running inside the graph invocation — this is what makes the five
    in-graph service clients' api_call_counter increments actually land
    (plan 12-13, Task 1)."""
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    await db_session.commit()

    captured_plan_id: str | None = None

    async def _capture_and_return(*_args, **_kwargs):
        nonlocal captured_plan_id
        captured_plan_id = get_current_plan_id()
        return _FINAL_STATE

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(side_effect=_capture_and_return)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db_session

    with (
        patch("app.workers.tasks.build_research_graph", return_value=mock_graph),
        patch(
            "app.workers.tasks.read_and_clear_api_call_count",
            new=AsyncMock(return_value=0),
        ),
        patch("app.workers.tasks.publish_memo_terminal", new=AsyncMock()),
        patch("app.workers.tasks.session_scope", _fake_session_scope),
    ):
        await _run_research_async(
            memo_id=str(memo.id),
            plan_id=str(plan.id),
            ticker="AAPL",
            user_id=str(owner.id),
        )

    assert captured_plan_id == str(plan.id)


async def test_run_research_async_ambient_plan_id_is_plan_id_not_memo_id(
    db_session: AsyncSession,
):
    """The ambient plan id observed inside the graph invocation is the run's
    ``plan_id`` argument, never the ``memo_id`` (plan 12-13, Task 1)."""
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    await db_session.commit()

    assert str(memo.id) != str(plan.id)

    captured_plan_id: str | None = None

    async def _capture_and_return(*_args, **_kwargs):
        nonlocal captured_plan_id
        captured_plan_id = get_current_plan_id()
        return _FINAL_STATE

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(side_effect=_capture_and_return)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db_session

    with (
        patch("app.workers.tasks.build_research_graph", return_value=mock_graph),
        patch(
            "app.workers.tasks.read_and_clear_api_call_count",
            new=AsyncMock(return_value=0),
        ),
        patch("app.workers.tasks.publish_memo_terminal", new=AsyncMock()),
        patch("app.workers.tasks.session_scope", _fake_session_scope),
    ):
        await _run_research_async(
            memo_id=str(memo.id),
            plan_id=str(plan.id),
            ticker="AAPL",
            user_id=str(owner.id),
        )

    assert captured_plan_id == str(plan.id)
    assert captured_plan_id != str(memo.id)


# ---------------------------------------------------------------------------
# MEMO-06/OBS-02 — cost aggregation (12-13 Task 2)
# ---------------------------------------------------------------------------


async def test_run_research_async_sums_tokens_across_agents(
    db_session: AsyncSession,
):
    """body["cost"]["tokens"] equals the sum of every prompt and completion
    count across this run's AgentOutput rows."""
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    await _seed_agent_output_with_tokens(db_session, plan, "FundamentalAnalysis", 10, 20)
    await _seed_agent_output_with_tokens(db_session, plan, "SentimentNLP", 5, 15)
    await _seed_agent_output_with_tokens(db_session, plan, "RiskAssessment", 7, 3)
    await db_session.commit()

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value=_FINAL_STATE)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db_session

    with (
        patch("app.workers.tasks.build_research_graph", return_value=mock_graph),
        patch(
            "app.workers.tasks.read_and_clear_api_call_count",
            new=AsyncMock(return_value=0),
        ),
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

    assert memo.body["cost"]["tokens"] == (10 + 20) + (5 + 15) + (7 + 3)


async def test_run_research_async_api_calls_equals_patched_counter_read(
    db_session: AsyncSession,
):
    """body["cost"]["api_calls"] equals the value returned by the patched
    counter read, and the read is awaited exactly once with the run's
    plan id."""
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    await db_session.commit()

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value=_FINAL_STATE)

    mock_read_and_clear = AsyncMock(return_value=42)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db_session

    with (
        patch("app.workers.tasks.build_research_graph", return_value=mock_graph),
        patch(
            "app.workers.tasks.read_and_clear_api_call_count",
            new=mock_read_and_clear,
        ),
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

    assert memo.body["cost"]["api_calls"] == 42
    mock_read_and_clear.assert_awaited_once_with(str(plan.id))


async def test_run_research_async_null_token_columns_contribute_zero(
    db_session: AsyncSession,
):
    """An AgentOutput row whose token columns are NULL contributes zero to
    the total and does not raise."""
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    await _seed_agent_output_with_tokens(db_session, plan, "FundamentalAnalysis", 10, 20)
    await _seed_agent_output_with_tokens(db_session, plan, "SentimentNLP", None, None)
    await db_session.commit()

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value=_FINAL_STATE)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db_session

    with (
        patch("app.workers.tasks.build_research_graph", return_value=mock_graph),
        patch(
            "app.workers.tasks.read_and_clear_api_call_count",
            new=AsyncMock(return_value=0),
        ),
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

    assert memo.body["cost"]["tokens"] == 30


async def test_run_research_async_rerun_excludes_older_agent_output(
    db_session: AsyncSession,
):
    """Two AgentTask rows for the same agent_type (a rerun) contribute only
    the newer row's tokens — the older run's tokens are not double-counted
    (D-03 rerun lineage)."""
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    # Older run's row for the same agent_type.
    await _seed_agent_output_with_tokens(db_session, plan, "FundamentalAnalysis", 1000, 2000)
    await db_session.commit()
    # This run's (newer) row for the same agent_type.
    await _seed_agent_output_with_tokens(db_session, plan, "FundamentalAnalysis", 10, 20)
    await db_session.commit()

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value=_FINAL_STATE)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db_session

    with (
        patch("app.workers.tasks.build_research_graph", return_value=mock_graph),
        patch(
            "app.workers.tasks.read_and_clear_api_call_count",
            new=AsyncMock(return_value=0),
        ),
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

    # Only the newer row's 10 + 20 = 30 counts, not the older 1000 + 2000.
    assert memo.body["cost"]["tokens"] == 30


async def test_run_research_async_all_failed_run_still_persists_zero_cost(
    db_session: AsyncSession,
):
    """A run in which every section failed still persists a cost key, with
    a zero token total rather than a missing key."""
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    await db_session.commit()

    final_state = {
        "fundamentals_output": None,
        "fundamentals_status": "FAILED",
        "sentiment_output": None,
        "sentiment_status": "FAILED",
        "risk_output": None,
        "risk_status": "FAILED",
        "macro_output": None,
        "macro_status": "FAILED",
        "comparables_output": None,
        "comparables_status": "FAILED",
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
        patch(
            "app.workers.tasks.read_and_clear_api_call_count",
            new=AsyncMock(return_value=0),
        ),
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

    assert memo.status == ResearchMemoStatus.FAILED
    assert memo.body["cost"] == {"tokens": 0, "api_calls": 0}


async def test_run_research_async_cost_has_exactly_two_keys(
    db_session: AsyncSession,
):
    """memo.body["cost"] has exactly the two keys tokens and api_calls —
    the shape never carries a monetary field (D-04)."""
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    await db_session.commit()

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value=_FINAL_STATE)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db_session

    with (
        patch("app.workers.tasks.build_research_graph", return_value=mock_graph),
        patch(
            "app.workers.tasks.read_and_clear_api_call_count",
            new=AsyncMock(return_value=0),
        ),
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

    assert set(memo.body["cost"].keys()) == {"tokens", "api_calls"}


async def test_run_research_async_zero_counter_read_still_yields_well_formed_cost(
    db_session: AsyncSession,
):
    """A counter read that returns 0 because Redis was unreachable still
    yields a well-formed cost value rather than a missing key."""
    owner = await _seed_user(db_session)
    await _seed_company(db_session)
    plan = await _seed_plan(db_session, owner)
    memo = await _seed_pending_memo(db_session, plan, owner)
    await _seed_agent_output_with_tokens(db_session, plan, "FundamentalAnalysis", 10, 20)
    await db_session.commit()

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value=_FINAL_STATE)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db_session

    with (
        patch("app.workers.tasks.build_research_graph", return_value=mock_graph),
        patch(
            "app.workers.tasks.read_and_clear_api_call_count",
            new=AsyncMock(return_value=0),
        ),
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

    assert memo.body["cost"] == {"tokens": 30, "api_calls": 0}
