"""Unit tests for ``sentiment_nlp_node`` (05-05-PLAN.md, D-02, D-07).

Coverage (AGENT-01, EXEC-04):
  - test_status_success_both_sources: news + arXiv both present =>
    AgentTask.status == SUCCESS, AgentOutput.completeness == FULL,
    sentiment_output.narrative non-empty, citations non-empty, section ==
    SECTION_SENTIMENT.
  - test_status_partial_arxiv_missing: arXiv empty, news present =>
    AgentTask.status == PARTIAL, missing_fields is a human-readable D-07
    sentence.
  - test_status_failed_both_empty: news + arXiv both empty =>
    AgentTask.status == FAILED, sentiment_output None, missing_fields the
    no-news D-07 sentence.
  - test_node_never_raises_on_llm_error: call_groq raises => node degrades to
    FAILED and does not propagate the exception.
  - test_one_agenttask_and_one_agentoutput_persisted: exactly one AgentTask
    (agent_type "SentimentNLP") and one AgentOutput persisted for the plan.

Mocks only at the SERVICE boundary — ``app.agents.sentiment_nlp.call_groq``,
``app.agents.sentiment_nlp.news_client.get_recent_articles``, and
``app.agents.sentiment_nlp.arxiv_client.search`` — never the underlying
httpx/Groq SDK directly (mirrors ``tests/agents/test_fundamental_analysis.py``'s
boundary-mock convention). ``session_scope`` is patched with a tiny
async-context-manager helper that yields the ``db_session`` fixture without
closing it, since the fixture owns create_all/drop_all lifecycle.
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
from app.ingestion.section_constants import SECTION_SENTIMENT

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


def _build_state(plan: ResearchPlan, user: User) -> dict:
    return {
        "ticker": "AAPL",
        "user_id": str(user.id),
        "plan_id": str(plan.id),
    }


def _make_article(title: str = "AAPL beats earnings estimates") -> dict:
    return {
        "title": title,
        "description": "A description of the article.",
        "content": "Full article content.",
        "url": "https://example.com/article",
        "source": "Example News",
        "published_at": "2026-07-01T00:00:00Z",
    }


def _make_paper(title: str = "Deep learning for equity forecasting") -> dict:
    return {
        "title": title,
        "abstract": "An abstract discussing forecasting methods.",
        "url": "https://arxiv.org/abs/1234.5678",
        "published": "2026-06-30T00:00:00Z",
    }


def _patched_session_scope(db_session: AsyncSession):
    """Return an async context manager that yields db_session without
    closing it — db_session fixture owns the create_all/drop_all lifecycle.
    """

    @asynccontextmanager
    async def _scope():
        yield db_session

    return _scope


# ---------------------------------------------------------------------------
# Status / coverage rule
# ---------------------------------------------------------------------------


async def test_status_success_both_sources(db_session: AsyncSession) -> None:
    """News + arXiv both present => SUCCESS + FULL, narrative/citations present."""
    from app.agents.sentiment_nlp import sentiment_nlp_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan, user)
    articles = [_make_article()]
    papers = [_make_paper()]

    with (
        patch(
            "app.agents.sentiment_nlp.session_scope",
            _patched_session_scope(db_session),
        ),
        patch(
            "app.agents.sentiment_nlp.news_client.get_recent_articles",
            AsyncMock(return_value=articles),
        ),
        patch(
            "app.agents.sentiment_nlp.arxiv_client.search",
            AsyncMock(return_value=papers),
        ),
        patch(
            "app.agents.sentiment_nlp.call_groq",
            AsyncMock(
                return_value="Sentiment: bullish\n\nAAPL shows strong momentum."
            ),
        ),
    ):
        result = await sentiment_nlp_node(state)

    assert result["sentiment_status"] == AgentTaskStatus.SUCCESS.value
    output = result["sentiment_output"]
    assert output["narrative"]
    assert output["citations"]
    assert output["section"] == SECTION_SENTIMENT

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


async def test_status_partial_arxiv_missing(db_session: AsyncSession) -> None:
    """arXiv empty, news present => PARTIAL, missing_fields a user-facing sentence."""
    from app.agents.sentiment_nlp import sentiment_nlp_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan, user)
    articles = [_make_article()]

    with (
        patch(
            "app.agents.sentiment_nlp.session_scope",
            _patched_session_scope(db_session),
        ),
        patch(
            "app.agents.sentiment_nlp.news_client.get_recent_articles",
            AsyncMock(return_value=articles),
        ),
        patch(
            "app.agents.sentiment_nlp.arxiv_client.search",
            AsyncMock(return_value=[]),
        ),
        patch(
            "app.agents.sentiment_nlp.call_groq",
            AsyncMock(
                return_value="Sentiment: neutral\n\nMixed signals for AAPL."
            ),
        ),
    ):
        result = await sentiment_nlp_node(state)

    assert result["sentiment_status"] == AgentTaskStatus.PARTIAL.value

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
    assert "unavailable" in output_row.missing_fields or (
        "no recent" in output_row.missing_fields
    )
    assert " " in output_row.missing_fields  # human-readable sentence, not enum


# ---------------------------------------------------------------------------


async def test_status_failed_both_empty(db_session: AsyncSession) -> None:
    """News + arXiv both empty => FAILED, sentiment_output None, no-news sentence."""
    from app.agents.sentiment_nlp import sentiment_nlp_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan, user)

    with (
        patch(
            "app.agents.sentiment_nlp.session_scope",
            _patched_session_scope(db_session),
        ),
        patch(
            "app.agents.sentiment_nlp.news_client.get_recent_articles",
            AsyncMock(return_value=[]),
        ),
        patch(
            "app.agents.sentiment_nlp.arxiv_client.search",
            AsyncMock(return_value=[]),
        ),
        patch(
            "app.agents.sentiment_nlp.call_groq",
            AsyncMock(return_value="unused"),
        ) as mock_call_groq,
    ):
        result = await sentiment_nlp_node(state)

    assert result["sentiment_status"] == AgentTaskStatus.FAILED.value
    assert result["sentiment_output"] is None
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
    assert "no recent news" in output_row.missing_fields


# ---------------------------------------------------------------------------


async def test_node_never_raises_on_llm_error(db_session: AsyncSession) -> None:
    """call_groq raising an exception never propagates; node degrades to FAILED."""
    from app.agents.sentiment_nlp import sentiment_nlp_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan, user)
    articles = [_make_article()]
    papers = [_make_paper()]

    with (
        patch(
            "app.agents.sentiment_nlp.session_scope",
            _patched_session_scope(db_session),
        ),
        patch(
            "app.agents.sentiment_nlp.news_client.get_recent_articles",
            AsyncMock(return_value=articles),
        ),
        patch(
            "app.agents.sentiment_nlp.arxiv_client.search",
            AsyncMock(return_value=papers),
        ),
        patch(
            "app.agents.sentiment_nlp.call_groq",
            AsyncMock(side_effect=RuntimeError("groq boom")),
        ),
    ):
        result = await sentiment_nlp_node(state)

    assert result["sentiment_status"] == "FAILED"
    assert result["sentiment_output"] is None

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
    assert "unavailable" in output_row.missing_fields


# ---------------------------------------------------------------------------
# Persistence cardinality
# ---------------------------------------------------------------------------


async def test_one_agenttask_and_one_agentoutput_persisted(
    db_session: AsyncSession,
) -> None:
    """Exactly one AgentTask (SentimentNLP) and one AgentOutput exist per run."""
    from app.agents.sentiment_nlp import sentiment_nlp_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan, user)
    articles = [_make_article()]
    papers = [_make_paper()]

    with (
        patch(
            "app.agents.sentiment_nlp.session_scope",
            _patched_session_scope(db_session),
        ),
        patch(
            "app.agents.sentiment_nlp.news_client.get_recent_articles",
            AsyncMock(return_value=articles),
        ),
        patch(
            "app.agents.sentiment_nlp.arxiv_client.search",
            AsyncMock(return_value=papers),
        ),
        patch(
            "app.agents.sentiment_nlp.call_groq",
            AsyncMock(
                return_value="Sentiment: bullish\n\nAAPL shows strong momentum."
            ),
        ),
    ):
        await sentiment_nlp_node(state)

    task_rows = (
        (
            await db_session.execute(
                select(AgentTask).where(
                    AgentTask.plan_id == plan.id,
                    AgentTask.agent_type == "SentimentNLP",
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
