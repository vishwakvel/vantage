"""Unit tests for ``fundamental_analysis_node`` (04-02-PLAN.md, D-01).

Coverage (MEMO-02, MEMO-03, EXEC-02):
  - test_citations_have_canonical_id: citations carry the source chunk's
    ``metadata["canonical_id"]``.
  - test_citations_have_quote: citations carry a non-empty ``quote`` equal to
    the source chunk's ``text``.
  - test_status_success_all_sections: chunks covering all four target
    sections (mda, financials, notes, risk_factors) => AgentTask.status ==
    SUCCESS and AgentOutput.completeness == FULL.
  - test_status_partial_missing_section: chunks covering only some target
    sections => AgentTask.status == PARTIAL, AgentOutput.completeness ==
    PARTIAL, missing_fields lists the absent section names.
  - test_status_failed_zero_chunks: hybrid_retrieve returns [] =>
    AgentTask.status == FAILED and fundamentals_output is None; no exception.
  - test_node_never_raises_on_llm_error: call_groq raises => node returns a
    state update with fundamentals_status "FAILED" and does NOT propagate.
  - test_one_agenttask_and_one_agentoutput_persisted: after a run, exactly
    one AgentTask (agent_type "FundamentalAnalysis") and one AgentOutput
    exist for the plan.

Mocks only at the SERVICE boundary — ``app.agents.fundamental_analysis.call_groq``
and ``app.agents.fundamental_analysis.hybrid_retrieve`` — never the groq SDK or
ChromaDB directly (mirrors ``tests/services/test_ticker_resolver.py``'s
boundary-mock convention).
"""

import uuid
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
from app.ingestion.section_constants import (
    SECTION_FINANCIALS,
    SECTION_MDA,
    SECTION_NOTES,
    SECTION_RISK_FACTORS,
)

pytestmark = pytest.mark.anyio

_ALL_TARGET_SECTIONS = (SECTION_MDA, SECTION_FINANCIALS, SECTION_NOTES, SECTION_RISK_FACTORS)


# ---------------------------------------------------------------------------
# Seed helpers — mirrors tests/api/test_research_api.py's User/ResearchPlan chain
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


# ---------------------------------------------------------------------------
# Fake chunk builder — matches hybrid_retrieve's documented return shape
# (app/ingestion/retriever.py:131-162)
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


def _all_section_chunks() -> list[dict]:
    return [_make_chunk(section) for section in _ALL_TARGET_SECTIONS]


async def _build_state(db_session: AsyncSession, plan: ResearchPlan, user: User) -> dict:
    return {
        "session": db_session,
        "ticker": "AAPL",
        "user_id": str(user.id),
        "plan_id": str(plan.id),
    }


# ---------------------------------------------------------------------------
# Citation shape (MEMO-02, MEMO-03)
# ---------------------------------------------------------------------------


async def test_citations_have_canonical_id(db_session: AsyncSession) -> None:
    """Each citation carries the source chunk's canonical_id."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value="A narrative about AAPL's fundamentals."),
        ),
    ):
        result = await fundamental_analysis_node(state)

    citations = result["fundamentals_output"]["citations"]
    assert len(citations) == len(chunks)
    expected_ids = {c["metadata"]["canonical_id"] for c in chunks}
    actual_ids = {c["canonical_id"] for c in citations}
    assert actual_ids == expected_ids
    for citation in citations:
        assert citation["canonical_id"]


# ---------------------------------------------------------------------------


async def test_citations_have_quote(db_session: AsyncSession) -> None:
    """Each citation carries a non-empty quote equal to the source chunk's text."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value="A narrative about AAPL's fundamentals."),
        ),
    ):
        result = await fundamental_analysis_node(state)

    citations = result["fundamentals_output"]["citations"]
    text_by_id = {c["id"]: c["text"] for c in chunks}
    for citation in citations:
        assert citation["quote"]
        assert citation["quote"] == text_by_id[citation["chunk_id"]]


# ---------------------------------------------------------------------------
# Status / coverage rule
# ---------------------------------------------------------------------------


async def test_status_success_all_sections(db_session: AsyncSession) -> None:
    """Chunks covering all four target sections => SUCCESS + FULL."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value="A narrative about AAPL's fundamentals."),
        ),
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_status"] == AgentTaskStatus.SUCCESS.value

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


async def test_status_partial_missing_section(db_session: AsyncSession) -> None:
    """Chunks covering only some target sections => PARTIAL + PARTIAL, missing_fields populated."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    # Only MDA and Financials present — Notes and Risk Factors missing.
    chunks = [_make_chunk(SECTION_MDA), _make_chunk(SECTION_FINANCIALS)]
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value="A narrative about AAPL's fundamentals."),
        ),
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_status"] == AgentTaskStatus.PARTIAL.value

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
    assert set(output_row.missing_fields) == {SECTION_NOTES, SECTION_RISK_FACTORS}


# ---------------------------------------------------------------------------


async def test_status_failed_zero_chunks(db_session: AsyncSession) -> None:
    """Zero retrieved chunks => AgentTask.status FAILED, fundamentals_output None, no exception."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = await _build_state(db_session, plan, user)

    with (
        patch("app.agents.fundamental_analysis.hybrid_retrieve", return_value=[]),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value="unused"),
        ) as mock_call_groq,
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_status"] == AgentTaskStatus.FAILED.value
    assert result["fundamentals_output"] is None
    mock_call_groq.assert_not_awaited()

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.FAILED


# ---------------------------------------------------------------------------


async def test_node_never_raises_on_llm_error(db_session: AsyncSession) -> None:
    """call_groq raising an exception never propagates; node degrades to FAILED."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(side_effect=RuntimeError("groq boom")),
        ),
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_status"] == "FAILED"
    assert result["fundamentals_output"] is None

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.FAILED


# ---------------------------------------------------------------------------
# Persistence cardinality
# ---------------------------------------------------------------------------


async def test_one_agenttask_and_one_agentoutput_persisted(db_session: AsyncSession) -> None:
    """Exactly one AgentTask (FundamentalAnalysis) and one AgentOutput exist per run."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value="A narrative about AAPL's fundamentals."),
        ),
    ):
        await fundamental_analysis_node(state)

    task_rows = (
        await db_session.execute(
            select(AgentTask).where(
                AgentTask.plan_id == plan.id,
                AgentTask.agent_type == "FundamentalAnalysis",
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
