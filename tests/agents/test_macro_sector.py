"""Unit tests for ``macro_sector_node`` (05-07-PLAN.md, AGENT-03, EXEC-04).

Coverage (D-07):
  - test_status_success_all_series: all MACRO_SERIES series return
    observations => AgentTask.status == SUCCESS, AgentOutput.completeness ==
    FULL, narrative non-empty, citations reference series, section ==
    SECTION_MACRO.
  - test_status_partial_missing_series: some series empty/raise =>
    AgentTask.status == PARTIAL, missing_fields carries the D-07
    partial-series sentence.
  - test_status_failed_zero_series: every series empty/raise =>
    AgentTask.status == FAILED, macro_output None, missing_fields carries
    the D-07 no-macro-data sentence.
  - test_node_never_raises_on_llm_error: call_groq raises => node returns a
    state update with macro_status "FAILED" and does NOT propagate.
  - test_one_agenttask_and_one_agentoutput_persisted: after a run, exactly
    one AgentTask (agent_type "MacroSector") and one AgentOutput exist for
    the plan.

Mocks only at the SERVICE boundary — ``app.agents.macro_sector.call_groq``,
``app.agents.macro_sector.fred_client.get_series_observations``, and
``app.agents.macro_sector.session_scope`` — never the groq SDK or httpx
directly (mirrors ``tests/agents/test_fundamental_analysis.py``'s
boundary-mock convention). No live network.
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
from app.ingestion.section_constants import SECTION_MACRO
from app.services.fred_client import MACRO_SERIES

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


def _session_scope_yielding(db_session: AsyncSession):
    """Return an async-context-manager factory that yields db_session, never
    closing it — mirrors the patched session_scope() convention so the
    fixture's own teardown (drop_all) still owns the session lifecycle.
    """

    @asynccontextmanager
    async def _scope():
        yield db_session

    return _scope


def _all_series_observations() -> list[dict]:
    """One fake observation per MACRO_SERIES label, most-recent-first."""
    return [{"date": "2026-06-01", "value": "5.33"}]


# ---------------------------------------------------------------------------
# SUCCESS — all series return data
# ---------------------------------------------------------------------------


async def test_status_success_all_series(db_session: AsyncSession) -> None:
    """All MACRO_SERIES series return observations => SUCCESS + FULL."""
    from app.agents.macro_sector import macro_sector_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan, user)

    with (
        patch(
            "app.agents.macro_sector.session_scope",
            _session_scope_yielding(db_session),
        ),
        patch(
            "app.agents.macro_sector.fred_client.get_series_observations",
            AsyncMock(return_value=_all_series_observations()),
        ),
        patch(
            "app.agents.macro_sector.call_groq",
            AsyncMock(return_value="A macro narrative contextualizing AAPL's sector."),
        ),
    ):
        result = await macro_sector_node(state)

    assert result["macro_status"] == AgentTaskStatus.SUCCESS.value
    output = result["macro_output"]
    assert output is not None
    assert output["narrative"]
    assert output["section"] == SECTION_MACRO
    assert len(output["citations"]) == len(MACRO_SERIES)
    citation_series_ids = {c["series_id"] for c in output["citations"]}
    assert citation_series_ids == set(MACRO_SERIES.values())

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
# PARTIAL — some series missing
# ---------------------------------------------------------------------------


async def test_status_partial_missing_series(db_session: AsyncSession) -> None:
    """Some series empty/raise => PARTIAL, missing_fields carries the D-07
    partial-series sentence."""
    from app.agents.macro_sector import _REASONS, macro_sector_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan, user)

    series_ids = list(MACRO_SERIES.values())
    call_count = {"n": 0}

    async def _side_effect(series_id: str, *, limit: int = 12) -> list[dict]:
        call_count["n"] += 1
        # First series id returns data, all others are empty (missing).
        if series_id == series_ids[0]:
            return _all_series_observations()
        return []

    with (
        patch(
            "app.agents.macro_sector.session_scope",
            _session_scope_yielding(db_session),
        ),
        patch(
            "app.agents.macro_sector.fred_client.get_series_observations",
            AsyncMock(side_effect=_side_effect),
        ),
        patch(
            "app.agents.macro_sector.call_groq",
            AsyncMock(return_value="A macro narrative with partial coverage."),
        ),
    ):
        result = await macro_sector_node(state)

    assert result["macro_status"] == AgentTaskStatus.PARTIAL.value
    assert result["macro_output"] is not None

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
    assert output_row.missing_fields == [_REASONS["partial_macro_data"]]


# ---------------------------------------------------------------------------
# FAILED — zero series return data
# ---------------------------------------------------------------------------


async def test_status_failed_zero_series(db_session: AsyncSession) -> None:
    """Every series empty/raises => FAILED, macro_output None, missing_fields
    carries the D-07 no-macro-data sentence; no exception propagates."""
    from app.agents.macro_sector import _REASONS, macro_sector_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan, user)

    async def _raises(series_id: str, *, limit: int = 12) -> list[dict]:
        raise RuntimeError("FRED down")

    with (
        patch(
            "app.agents.macro_sector.session_scope",
            _session_scope_yielding(db_session),
        ),
        patch(
            "app.agents.macro_sector.fred_client.get_series_observations",
            AsyncMock(side_effect=_raises),
        ),
        patch(
            "app.agents.macro_sector.call_groq",
            AsyncMock(return_value="unused"),
        ) as mock_call_groq,
    ):
        result = await macro_sector_node(state)

    assert result["macro_status"] == AgentTaskStatus.FAILED.value
    assert result["macro_output"] is None
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
    assert output_row.missing_fields == [_REASONS["no_macro_data"]]


# ---------------------------------------------------------------------------
# Never-raises on LLM error
# ---------------------------------------------------------------------------


async def test_node_never_raises_on_llm_error(db_session: AsyncSession) -> None:
    """call_groq raising an exception never propagates; node degrades to
    FAILED with the D-07 llm-error sentence."""
    from app.agents.macro_sector import _REASONS, macro_sector_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan, user)

    with (
        patch(
            "app.agents.macro_sector.session_scope",
            _session_scope_yielding(db_session),
        ),
        patch(
            "app.agents.macro_sector.fred_client.get_series_observations",
            AsyncMock(return_value=_all_series_observations()),
        ),
        patch(
            "app.agents.macro_sector.call_groq",
            AsyncMock(side_effect=RuntimeError("groq boom")),
        ),
    ):
        result = await macro_sector_node(state)

    assert result["macro_status"] == "FAILED"
    assert result["macro_output"] is None

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
    assert output_row.missing_fields == [_REASONS["llm_error"]]


# ---------------------------------------------------------------------------
# Persistence cardinality
# ---------------------------------------------------------------------------


async def test_one_agenttask_and_one_agentoutput_persisted(db_session: AsyncSession) -> None:
    """Exactly one AgentTask (MacroSector) and one AgentOutput exist per run."""
    from app.agents.macro_sector import macro_sector_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_state(plan, user)

    with (
        patch(
            "app.agents.macro_sector.session_scope",
            _session_scope_yielding(db_session),
        ),
        patch(
            "app.agents.macro_sector.fred_client.get_series_observations",
            AsyncMock(return_value=_all_series_observations()),
        ),
        patch(
            "app.agents.macro_sector.call_groq",
            AsyncMock(return_value="A macro narrative contextualizing AAPL's sector."),
        ),
    ):
        await macro_sector_node(state)

    task_rows = (
        await db_session.execute(
            select(AgentTask).where(
                AgentTask.plan_id == plan.id,
                AgentTask.agent_type == "MacroSector",
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
