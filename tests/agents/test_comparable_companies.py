"""Unit tests for ``comparable_companies_node`` (05-08-PLAN.md).

Coverage (AGENT-04, EXEC-04, D-05, D-07):
  - test_status_success_all_peer_metrics: peers + full metrics for every peer
    => AgentTask.status == SUCCESS, AgentOutput.completeness == FULL,
    narrative non-empty, peers listed, section == SECTION_COMPARABLES.
  - test_status_partial_missing_some_metrics: peers present but metrics
    missing for one or more peers => AgentTask.status == PARTIAL,
    missing_fields carries the partial-metrics D-07 sentence.
  - test_status_failed_empty_peers: comparables_source.get_peers returns []
    => AgentTask.status == FAILED, comparables_output None, missing_fields
    carries the no-peers D-07 sentence.
  - test_node_never_raises_on_llm_error: call_groq raises => node returns a
    state update with comparables_status "FAILED" and does NOT propagate.
  - test_one_agenttask_and_one_agentoutput_persisted: after a run, exactly
    one AgentTask (agent_type "ComparableCompanies") and one AgentOutput
    exist for the plan.

Mocks only at the SERVICE boundary — ``app.agents.comparable_companies.call_groq``,
``app.agents.comparable_companies.comparables_source.get_peers``,
``app.agents.comparable_companies.comparables_source.get_metrics``, and
``app.agents.comparable_companies.session_scope`` (patched to yield the
test's own ``db_session`` fixture without closing it) — never the yfinance
SDK or groq SDK directly (mirrors
``tests/agents/test_fundamental_analysis.py``'s boundary-mock convention).
No live network calls.
"""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AgentOutput,
    AgentOutputCompleteness,
    AgentTask,
    AgentTaskStatus,
    ResearchPlan,
    ResearchRequest,
    User,
)
from app.ingestion.section_constants import SECTION_COMPARABLES

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Seed helpers — mirrors tests/agents/test_fundamental_analysis.py's
# User/ResearchRequest/ResearchPlan chain
# ---------------------------------------------------------------------------


async def _seed_user(db_session: AsyncSession) -> User:
    user = User(
        email=f"{uuid.uuid4()}@example.com",
        password_hash="not-a-real-hash",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


async def _seed_plan(db_session: AsyncSession, owner: User) -> ResearchPlan:
    request = ResearchRequest(
        user_id=owner.id, raw_query="Tell me about Apple", status="RESOLVED"
    )
    db_session.add(request)
    await db_session.flush()

    plan = ResearchPlan(
        request_id=request.id,
        user_id=owner.id,
        resolved_tickers=["AAPL"],
    )
    db_session.add(plan)
    await db_session.flush()
    await db_session.refresh(plan)
    return plan


def _build_state(plan: ResearchPlan) -> dict:
    return {"ticker": "AAPL", "plan_id": str(plan.id)}


def _patched_session_scope(db_session: AsyncSession):
    """Async context manager yielding the test's own db_session, never
    closing it — patches ``session_scope`` so the node's own-session
    contract runs against the test's transactional fixture instead of
    opening a real second connection.
    """

    @asynccontextmanager
    async def _scope():
        yield db_session

    return _scope


def _make_metrics(tickers: list[str]) -> list[dict]:
    return [
        {
            "ticker": t,
            "market_cap": 1_000_000_000,
            "trailing_pe": 15.5,
            "profit_margin": 0.2,
            "revenue": 500_000_000,
        }
        for t in tickers
    ]


# ---------------------------------------------------------------------------
# SUCCESS
# ---------------------------------------------------------------------------


async def test_status_success_all_peer_metrics(db_session: AsyncSession) -> None:
    """Peers found with metrics for every peer => SUCCESS + FULL."""
    from app.agents.comparable_companies import comparable_companies_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan)
    peers = ["MSFT", "GOOGL", "AMZN"]
    metrics = _make_metrics(peers)

    with (
        patch(
            "app.agents.comparable_companies.session_scope",
            side_effect=_patched_session_scope(db_session),
        ),
        patch(
            "app.agents.comparable_companies.comparables_source.get_peers",
            AsyncMock(return_value=peers),
        ),
        patch(
            "app.agents.comparable_companies.comparables_source.get_metrics",
            AsyncMock(return_value=metrics),
        ),
        patch(
            "app.agents.comparable_companies.call_groq",
            AsyncMock(return_value="AAPL trades at a premium to its peers."),
        ),
    ):
        result = await comparable_companies_node(state)

    assert result["comparables_status"] == AgentTaskStatus.SUCCESS.value
    output = result["comparables_output"]
    assert output["narrative"]
    assert output["peers"] == peers
    assert output["section"] == SECTION_COMPARABLES
    assert {c["ticker"] for c in output["citations"]} == set(peers)

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.SUCCESS

    output_row = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_row.id)
        )
    ).scalar_one()
    assert output_row.completeness == AgentOutputCompleteness.FULL
    assert output_row.missing_fields is None


# ---------------------------------------------------------------------------
# PARTIAL
# ---------------------------------------------------------------------------


async def test_status_partial_missing_some_metrics(db_session: AsyncSession) -> None:
    """Peers found but metrics missing for some => PARTIAL, missing_fields sentence."""
    from app.agents.comparable_companies import comparable_companies_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan)
    peers = ["MSFT", "GOOGL", "AMZN"]
    # Metrics only fetched for MSFT — GOOGL and AMZN fetches were skipped.
    metrics = _make_metrics(["MSFT"])

    with (
        patch(
            "app.agents.comparable_companies.session_scope",
            side_effect=_patched_session_scope(db_session),
        ),
        patch(
            "app.agents.comparable_companies.comparables_source.get_peers",
            AsyncMock(return_value=peers),
        ),
        patch(
            "app.agents.comparable_companies.comparables_source.get_metrics",
            AsyncMock(return_value=metrics),
        ),
        patch(
            "app.agents.comparable_companies.call_groq",
            AsyncMock(return_value="AAPL trades at a premium to MSFT."),
        ),
    ):
        result = await comparable_companies_node(state)

    assert result["comparables_status"] == AgentTaskStatus.PARTIAL.value

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.PARTIAL

    output_row = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_row.id)
        )
    ).scalar_one()
    assert output_row.completeness == AgentOutputCompleteness.PARTIAL
    assert output_row.missing_fields
    assert "metrics unavailable for some peers" in output_row.missing_fields


# ---------------------------------------------------------------------------
# FAILED — empty peer set
# ---------------------------------------------------------------------------


async def test_status_failed_empty_peers(db_session: AsyncSession) -> None:
    """Empty peer set => FAILED, comparables_output None, no-peers D-07 sentence."""
    from app.agents.comparable_companies import comparable_companies_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan)

    with (
        patch(
            "app.agents.comparable_companies.session_scope",
            side_effect=_patched_session_scope(db_session),
        ),
        patch(
            "app.agents.comparable_companies.comparables_source.get_peers",
            AsyncMock(return_value=[]),
        ),
        patch(
            "app.agents.comparable_companies.comparables_source.get_metrics",
            AsyncMock(return_value=[]),
        ) as mock_get_metrics,
        patch(
            "app.agents.comparable_companies.call_groq",
            AsyncMock(return_value="unused"),
        ) as mock_call_groq,
    ):
        result = await comparable_companies_node(state)

    assert result["comparables_status"] == AgentTaskStatus.FAILED.value
    assert result["comparables_output"] is None
    mock_get_metrics.assert_not_awaited()
    mock_call_groq.assert_not_awaited()

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.FAILED

    output_row = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_row.id)
        )
    ).scalar_one()
    assert output_row.completeness == AgentOutputCompleteness.PARTIAL
    assert "no peer set could be constructed for AAPL" in output_row.missing_fields


# ---------------------------------------------------------------------------
# Never-raises on LLM error
# ---------------------------------------------------------------------------


async def test_node_never_raises_on_llm_error(db_session: AsyncSession) -> None:
    """call_groq raising an exception never propagates; node degrades to FAILED."""
    from app.agents.comparable_companies import comparable_companies_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan)
    peers = ["MSFT", "GOOGL"]
    metrics = _make_metrics(peers)

    with (
        patch(
            "app.agents.comparable_companies.session_scope",
            side_effect=_patched_session_scope(db_session),
        ),
        patch(
            "app.agents.comparable_companies.comparables_source.get_peers",
            AsyncMock(return_value=peers),
        ),
        patch(
            "app.agents.comparable_companies.comparables_source.get_metrics",
            AsyncMock(return_value=metrics),
        ),
        patch(
            "app.agents.comparable_companies.call_groq",
            AsyncMock(side_effect=RuntimeError("groq boom")),
        ),
    ):
        result = await comparable_companies_node(state)

    assert result["comparables_status"] == "FAILED"
    assert result["comparables_output"] is None

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.FAILED

    output_row = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_row.id)
        )
    ).scalar_one()
    assert output_row.missing_fields == "Comparable-companies analysis unavailable — analysis engine error"


# ---------------------------------------------------------------------------
# Persistence cardinality
# ---------------------------------------------------------------------------


async def test_one_agenttask_and_one_agentoutput_persisted(db_session: AsyncSession) -> None:
    """Exactly one AgentTask (ComparableCompanies) and one AgentOutput exist per run."""
    from app.agents.comparable_companies import comparable_companies_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan)
    peers = ["MSFT", "GOOGL"]
    metrics = _make_metrics(peers)

    with (
        patch(
            "app.agents.comparable_companies.session_scope",
            side_effect=_patched_session_scope(db_session),
        ),
        patch(
            "app.agents.comparable_companies.comparables_source.get_peers",
            AsyncMock(return_value=peers),
        ),
        patch(
            "app.agents.comparable_companies.comparables_source.get_metrics",
            AsyncMock(return_value=metrics),
        ),
        patch(
            "app.agents.comparable_companies.call_groq",
            AsyncMock(return_value="AAPL trades at a premium to its peers."),
        ),
    ):
        await comparable_companies_node(state)

    task_rows = (
        await db_session.execute(
            select(AgentTask).where(
                AgentTask.plan_id == plan.id,
                AgentTask.agent_type == "ComparableCompanies",
            )
        )
    ).scalars().all()
    assert len(task_rows) == 1

    output_rows = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_rows[0].id)
        )
    ).scalars().all()
    assert len(output_rows) == 1
