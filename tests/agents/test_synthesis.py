"""Unit tests for ``synthesis_node`` and ``_compute_memo_status`` (04-03-PLAN.md, D-02).

Coverage (EXEC-02, EXEC-03):
  - test_synthesis_reads_fundamentals_output: synthesis prompt/output
    references the fundamentals findings; synthesis_output contains a
    non-empty ``take`` and ``section == "synthesis"``.
  - test_memo_status_complete: fundamentals SUCCESS + synthesis SUCCESS =>
    memo_status == "COMPLETE".
  - test_memo_status_partial_on_fundamentals_failed: fundamentals FAILED +
    synthesis SUCCESS => memo_status == "PARTIAL" (EXEC-03).
  - test_memo_status_partial_on_partial_agent: fundamentals PARTIAL +
    synthesis SUCCESS => memo_status == "PARTIAL".
  - test_memo_status_failed_when_both_fail: fundamentals FAILED + synthesis
    FAILED => memo_status == "FAILED".
  - test_synthesis_never_raises: call_groq raises => synthesis_status
    "FAILED", node returns a state update, does not propagate; memo_status
    still computed.
  - test_one_agenttask_and_one_agentoutput_persisted: exactly one AgentTask
    (agent_type "Synthesis") + one AgentOutput after a run.

Mocks only at the SERVICE boundary — ``app.agents.synthesis.call_groq`` —
never the groq SDK directly (mirrors ``tests/agents/test_fundamental_analysis.py``'s
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


def _fundamentals_output() -> dict:
    return {
        "narrative": (
            "AAPL revenue grew 8% YoY with expanding gross margins; balance "
            "sheet remains strong with low net debt."
        ),
        "citations": [
            {
                "canonical_id": "canon-mda-1",
                "chunk_id": str(uuid.uuid4()),
                "section": "mda",
                "quote": "Revenue grew 8% year over year.",
                "form_type": "10-K",
                "period_of_report": "2025-12-31",
            }
        ],
        "section": "fundamentals",
    }


async def _build_state(
    db_session: AsyncSession,
    plan: ResearchPlan,
    fundamentals_output: dict | None,
    fundamentals_status: str,
) -> dict:
    return {
        "session": db_session,
        "ticker": "AAPL",
        "plan_id": str(plan.id),
        "fundamentals_output": fundamentals_output,
        "fundamentals_status": fundamentals_status,
    }


# ---------------------------------------------------------------------------
# Synthesis reads fundamentals_output and produces a distinct take
# ---------------------------------------------------------------------------


async def test_synthesis_reads_fundamentals_output(db_session: AsyncSession) -> None:
    """Synthesis prompt references fundamentals findings; output has a take."""
    from app.agents.synthesis import synthesis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    fundamentals_output = _fundamentals_output()
    state = await _build_state(db_session, plan, fundamentals_output, "SUCCESS")

    captured_prompt = None

    async def _fake_call_groq(prompt: str, **kwargs: object) -> str:
        nonlocal captured_prompt
        captured_prompt = prompt
        return "Overall, AAPL presents a compelling investment case."

    with patch(
        "app.agents.synthesis.call_groq", AsyncMock(side_effect=_fake_call_groq)
    ):
        result = await synthesis_node(state)

    assert captured_prompt is not None
    assert "8% year over year" in captured_prompt or "8%" in captured_prompt

    synthesis_output = result["synthesis_output"]
    assert synthesis_output["section"] == "synthesis"
    assert synthesis_output["take"]
    assert synthesis_output["take"].strip() != ""


# ---------------------------------------------------------------------------
# Memo-status rule (D-02 ownership)
# ---------------------------------------------------------------------------


async def test_memo_status_complete(db_session: AsyncSession) -> None:
    """fundamentals SUCCESS + synthesis SUCCESS => memo_status COMPLETE."""
    from app.agents.synthesis import synthesis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = await _build_state(
        db_session, plan, _fundamentals_output(), "SUCCESS"
    )

    with patch(
        "app.agents.synthesis.call_groq",
        AsyncMock(return_value="An overall take."),
    ):
        result = await synthesis_node(state)

    assert result["synthesis_status"] == AgentTaskStatus.SUCCESS.value
    assert result["memo_status"] == "COMPLETE"


async def test_memo_status_partial_on_fundamentals_failed(
    db_session: AsyncSession,
) -> None:
    """fundamentals FAILED + synthesis SUCCESS => memo_status PARTIAL (EXEC-03)."""
    from app.agents.synthesis import synthesis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = await _build_state(db_session, plan, None, "FAILED")

    with patch(
        "app.agents.synthesis.call_groq",
        AsyncMock(return_value="An overall take despite missing fundamentals."),
    ):
        result = await synthesis_node(state)

    assert result["synthesis_status"] == AgentTaskStatus.SUCCESS.value
    assert result["memo_status"] == "PARTIAL"


async def test_memo_status_partial_on_partial_agent(db_session: AsyncSession) -> None:
    """fundamentals PARTIAL + synthesis SUCCESS => memo_status PARTIAL."""
    from app.agents.synthesis import synthesis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = await _build_state(
        db_session, plan, _fundamentals_output(), "PARTIAL"
    )

    with patch(
        "app.agents.synthesis.call_groq",
        AsyncMock(return_value="An overall take."),
    ):
        result = await synthesis_node(state)

    assert result["synthesis_status"] == AgentTaskStatus.SUCCESS.value
    assert result["memo_status"] == "PARTIAL"


async def test_memo_status_failed_when_both_fail(db_session: AsyncSession) -> None:
    """fundamentals FAILED + synthesis FAILED => memo_status FAILED."""
    from app.agents.synthesis import synthesis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = await _build_state(db_session, plan, None, "FAILED")

    with patch(
        "app.agents.synthesis.call_groq",
        AsyncMock(side_effect=RuntimeError("groq boom")),
    ):
        result = await synthesis_node(state)

    assert result["synthesis_status"] == "FAILED"
    assert result["memo_status"] == "FAILED"


# ---------------------------------------------------------------------------
# Never-raise contract
# ---------------------------------------------------------------------------


async def test_synthesis_never_raises(db_session: AsyncSession) -> None:
    """call_groq raising an exception never propagates; node degrades to FAILED."""
    from app.agents.synthesis import synthesis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = await _build_state(
        db_session, plan, _fundamentals_output(), "SUCCESS"
    )

    with patch(
        "app.agents.synthesis.call_groq",
        AsyncMock(side_effect=RuntimeError("groq boom")),
    ):
        result = await synthesis_node(state)

    assert result["synthesis_status"] == "FAILED"
    assert result["synthesis_output"] is None
    # fundamentals SUCCESS but synthesis FAILED => PARTIAL, never masked as COMPLETE
    assert result["memo_status"] == "PARTIAL"

    task_row = (
        await db_session.execute(
            select(AgentTask).where(
                AgentTask.plan_id == plan.id, AgentTask.agent_type == "Synthesis"
            )
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.FAILED


# ---------------------------------------------------------------------------
# Persistence cardinality
# ---------------------------------------------------------------------------


async def test_one_agenttask_and_one_agentoutput_persisted(
    db_session: AsyncSession,
) -> None:
    """Exactly one AgentTask (Synthesis) and one AgentOutput exist per run."""
    from app.agents.synthesis import synthesis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = await _build_state(
        db_session, plan, _fundamentals_output(), "SUCCESS"
    )

    with patch(
        "app.agents.synthesis.call_groq",
        AsyncMock(return_value="An overall take."),
    ):
        await synthesis_node(state)

    task_rows = (
        await db_session.execute(
            select(AgentTask).where(
                AgentTask.plan_id == plan.id,
                AgentTask.agent_type == "Synthesis",
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
    assert output_rows[0].completeness == AgentOutputCompleteness.FULL
