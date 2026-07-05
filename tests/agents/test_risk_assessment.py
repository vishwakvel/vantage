"""Unit tests for ``risk_assessment_node`` (05-06-PLAN.md, D-03).

Coverage (AGENT-02, EXEC-04, D-07):
  - test_status_success_chunks_and_news: Risk Factors chunks + news present
    => AgentTask.status == SUCCESS, AgentOutput.completeness == FULL,
    narrative non-empty, categories == the three risk categories, output
    section == SECTION_RISKS.
  - test_status_partial_news_missing: chunks present, news empty => PARTIAL,
    missing_fields carries the news-missing D-07 sentence.
  - test_status_failed_zero_chunks: zero Risk Factors chunks => FAILED,
    risk_output None, missing_fields carries the no-risk-factors D-07
    sentence.
  - test_node_never_raises_on_llm_error: call_groq raises => FAILED, no
    exception propagates.
  - test_one_agenttask_and_one_agentoutput_persisted: exactly one AgentTask
    (agent_type "RiskAssessment") + one AgentOutput exist per run.

Mocks only at the module boundary — ``app.agents.risk_assessment.call_groq``,
``app.agents.risk_assessment.hybrid_retrieve``,
``app.agents.risk_assessment.news_client.get_recent_articles``, and
``app.agents.risk_assessment.session_scope`` — never the groq SDK, httpx, or
ChromaDB directly (mirrors ``tests/agents/test_fundamental_analysis.py``'s
boundary-mock convention).
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
from app.ingestion.section_constants import SECTION_RISK_FACTORS, SECTION_RISKS

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Seed helpers — mirrors tests/agents/test_fundamental_analysis.py
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
        user_id=owner.id, raw_query="Tell me about Apple's risks", status="RESOLVED"
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


# ---------------------------------------------------------------------------
# Fake chunk / article builders
# ---------------------------------------------------------------------------


def _make_chunk(section: str, canonical_id: str | None = None, text: str | None = None) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "text": text or f"Sample {section} narrative text discussing AAPL.",
        "metadata": {
            "canonical_id": canonical_id or f"canon-{section}-{uuid.uuid4()}",
            "section": section,
            "ticker": "AAPL",
            "form_type": "10-K",
            "period_of_report": "2025-12-31",
        },
        "score": 0.9,
    }


def _risk_factor_chunks() -> list[dict]:
    return [_make_chunk(SECTION_RISK_FACTORS) for _ in range(3)]


def _make_article(title: str = "AAPL faces new regulatory scrutiny") -> dict:
    return {
        "title": title,
        "description": "A description of an emerging risk.",
        "content": "Full article content.",
        "url": "https://example.com/article",
        "source": "Example News",
        "published_at": "2026-07-01T00:00:00Z",
    }


def _build_state(plan: ResearchPlan, user: User) -> dict:
    return {
        "ticker": "AAPL",
        "user_id": str(user.id),
        "plan_id": str(plan.id),
    }


def _patched_session_scope(db_session: AsyncSession):
    """Return an async context manager yielding db_session without closing it."""

    @asynccontextmanager
    async def _scope():
        yield db_session

    return _scope


# ---------------------------------------------------------------------------
# SUCCESS: chunks + news present
# ---------------------------------------------------------------------------


async def test_status_success_chunks_and_news(db_session: AsyncSession) -> None:
    """Risk Factors chunks + news present => SUCCESS + FULL, categories present."""
    from app.agents.risk_assessment import risk_assessment_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _risk_factor_chunks()
    articles = [_make_article()]
    state = _build_state(plan, user)

    with (
        patch(
            "app.agents.risk_assessment.session_scope",
            _patched_session_scope(db_session),
        ),
        patch("app.agents.risk_assessment.hybrid_retrieve", return_value=chunks),
        patch(
            "app.agents.risk_assessment.news_client.get_recent_articles",
            AsyncMock(return_value=articles),
        ),
        patch(
            "app.agents.risk_assessment.call_groq",
            AsyncMock(return_value="A structured risk narrative about AAPL."),
        ),
    ):
        result = await risk_assessment_node(state)

    assert result["risk_status"] == AgentTaskStatus.SUCCESS.value
    output = result["risk_output"]
    assert output["narrative"]
    assert output["section"] == SECTION_RISKS
    assert set(output["categories"]) == {
        "market/liquidity",
        "legal/regulatory",
        "operational",
    }
    assert len(output["citations"]) == len(chunks)

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.SUCCESS
    assert task_row.agent_type == "RiskAssessment"

    output_row = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_row.id)
        )
    ).scalar_one()
    assert output_row.completeness == AgentOutputCompleteness.FULL
    assert output_row.missing_fields is None


# ---------------------------------------------------------------------------
# PARTIAL: chunks present, news empty
# ---------------------------------------------------------------------------


async def test_status_partial_news_missing(db_session: AsyncSession) -> None:
    """Chunks present, news empty => PARTIAL, missing_fields has the D-07 sentence."""
    from app.agents.risk_assessment import risk_assessment_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _risk_factor_chunks()
    state = _build_state(plan, user)

    with (
        patch(
            "app.agents.risk_assessment.session_scope",
            _patched_session_scope(db_session),
        ),
        patch("app.agents.risk_assessment.hybrid_retrieve", return_value=chunks),
        patch(
            "app.agents.risk_assessment.news_client.get_recent_articles",
            AsyncMock(return_value=[]),
        ),
        patch(
            "app.agents.risk_assessment.call_groq",
            AsyncMock(return_value="A structured risk narrative about AAPL."),
        ),
    ):
        result = await risk_assessment_node(state)

    assert result["risk_status"] == AgentTaskStatus.PARTIAL.value

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
    assert output_row.missing_fields == [
        "Risk assessment based on filings only — no recent news found for AAPL"
    ]


# ---------------------------------------------------------------------------
# FAILED: zero chunks
# ---------------------------------------------------------------------------


async def test_status_failed_zero_chunks(db_session: AsyncSession) -> None:
    """Zero Risk Factors chunks => FAILED, risk_output None, D-07 sentence, no exception."""
    from app.agents.risk_assessment import risk_assessment_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan, user)

    with (
        patch(
            "app.agents.risk_assessment.session_scope",
            _patched_session_scope(db_session),
        ),
        patch("app.agents.risk_assessment.hybrid_retrieve", return_value=[]),
        patch(
            "app.agents.risk_assessment.news_client.get_recent_articles",
            AsyncMock(return_value=[]),
        ) as mock_news,
        patch(
            "app.agents.risk_assessment.call_groq",
            AsyncMock(return_value="unused"),
        ) as mock_call_groq,
    ):
        result = await risk_assessment_node(state)

    assert result["risk_status"] == AgentTaskStatus.FAILED.value
    assert result["risk_output"] is None
    mock_call_groq.assert_not_awaited()
    mock_news.assert_not_awaited()

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
    assert output_row.missing_fields == [
        "Risk assessment unavailable — no risk-factors disclosure found for AAPL"
    ]


# ---------------------------------------------------------------------------
# call_groq raises — never propagates
# ---------------------------------------------------------------------------


async def test_node_never_raises_on_llm_error(db_session: AsyncSession) -> None:
    """call_groq raising an exception never propagates; node degrades to FAILED."""
    from app.agents.risk_assessment import risk_assessment_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _risk_factor_chunks()
    state = _build_state(plan, user)

    with (
        patch(
            "app.agents.risk_assessment.session_scope",
            _patched_session_scope(db_session),
        ),
        patch("app.agents.risk_assessment.hybrid_retrieve", return_value=chunks),
        patch(
            "app.agents.risk_assessment.news_client.get_recent_articles",
            AsyncMock(return_value=[_make_article()]),
        ),
        patch(
            "app.agents.risk_assessment.call_groq",
            AsyncMock(side_effect=RuntimeError("groq boom")),
        ),
    ):
        result = await risk_assessment_node(state)

    assert result["risk_status"] == "FAILED"
    assert result["risk_output"] is None

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
    assert output_row.missing_fields == [
        "Risk assessment unavailable — analysis engine error"
    ]


# ---------------------------------------------------------------------------
# Persistence cardinality
# ---------------------------------------------------------------------------


async def test_one_agenttask_and_one_agentoutput_persisted(db_session: AsyncSession) -> None:
    """Exactly one AgentTask (RiskAssessment) and one AgentOutput exist per run."""
    from app.agents.risk_assessment import risk_assessment_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _risk_factor_chunks()
    state = _build_state(plan, user)

    with (
        patch(
            "app.agents.risk_assessment.session_scope",
            _patched_session_scope(db_session),
        ),
        patch("app.agents.risk_assessment.hybrid_retrieve", return_value=chunks),
        patch(
            "app.agents.risk_assessment.news_client.get_recent_articles",
            AsyncMock(return_value=[_make_article()]),
        ),
        patch(
            "app.agents.risk_assessment.call_groq",
            AsyncMock(return_value="A structured risk narrative about AAPL."),
        ),
    ):
        await risk_assessment_node(state)

    task_rows = (
        (
            await db_session.execute(
                select(AgentTask).where(
                    AgentTask.plan_id == plan.id,
                    AgentTask.agent_type == "RiskAssessment",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(task_rows) == 1

    output_rows = (
        (
            await db_session.execute(
                select(AgentOutput).where(AgentOutput.task_id == task_rows[0].id)
            )
        )
        .scalars()
        .all()
    )
    assert len(output_rows) == 1
