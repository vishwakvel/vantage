"""Unit tests for ``synthesis_node`` and ``_compute_memo_status``
(04-03-PLAN.md D-02, generalized to all 6 agents by 05-10-PLAN.md).

Coverage (EXEC-02, EXEC-03, EXEC-04, AGENT-06):
  - test_synthesis_reads_fundamentals_output: synthesis prompt/output
    references the fundamentals findings; synthesis_output contains a
    non-empty ``take`` and ``section == "synthesis"``.
  - test_memo_status_complete_all_six_success: all 5 specialists SUCCESS +
    synthesis SUCCESS => memo_status == "COMPLETE".
  - test_memo_status_partial_on_one_specialist_failed: one specialist FAILED,
    the other 4 + synthesis SUCCESS => memo_status == "PARTIAL" (EXEC-04).
  - test_memo_status_partial_on_partial_specialist: one specialist PARTIAL,
    the other 4 + synthesis SUCCESS => memo_status == "PARTIAL".
  - test_memo_status_failed_when_all_six_fail: all 5 specialists FAILED +
    synthesis FAILED => memo_status == "FAILED".
  - test_synthesis_never_raises: call_groq raises => synthesis_status
    "FAILED", node returns a state update, does not propagate; memo_status
    still computed.
  - test_one_agenttask_and_one_agentoutput_persisted: exactly one AgentTask
    (agent_type "Synthesis") + one AgentOutput after a run.
  - test_build_prompt_embeds_all_five_none_guarded: prompt embeds a
    None-guarded block per specialist source.

Mocks only at the SERVICE boundary — ``app.agents.synthesis.call_groq`` —
never the groq SDK directly (mirrors ``tests/agents/test_fundamental_analysis.py``'s
boundary-mock convention).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.synthesis import _build_prompt, _compute_memo_status
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


def _specialist_output(section: str, narrative: str) -> dict:
    return {"narrative": narrative, "citations": [], "section": section}


#: A generic non-None output for the 4 non-fundamentals specialists, used
#: when a test only cares about status combinations, not narrative content.
def _generic_outputs() -> dict:
    return {
        "sentiment_output": _specialist_output("sentiment", "Sentiment take."),
        "risk_output": _specialist_output("risks", "Risk take."),
        "macro_output": _specialist_output("macro", "Macro take."),
        "comparables_output": _specialist_output(
            "comparables", "Comparables take."
        ),
    }


async def _build_state(
    db_session: AsyncSession,
    plan: ResearchPlan,
    *,
    fundamentals_output: dict | None,
    fundamentals_status: str,
    sentiment_output: dict | None = None,
    sentiment_status: str = "FAILED",
    risk_output: dict | None = None,
    risk_status: str = "FAILED",
    macro_output: dict | None = None,
    macro_status: str = "FAILED",
    comparables_output: dict | None = None,
    comparables_status: str = "FAILED",
) -> dict:
    return {
        "session": db_session,
        "ticker": "AAPL",
        "plan_id": str(plan.id),
        "fundamentals_output": fundamentals_output,
        "fundamentals_status": fundamentals_status,
        "sentiment_output": sentiment_output,
        "sentiment_status": sentiment_status,
        "risk_output": risk_output,
        "risk_status": risk_status,
        "macro_output": macro_output,
        "macro_status": macro_status,
        "comparables_output": comparables_output,
        "comparables_status": comparables_status,
    }


async def _build_all_success_state(
    db_session: AsyncSession, plan: ResearchPlan
) -> dict:
    """All 5 specialists SUCCESS with non-None outputs."""
    generic = _generic_outputs()
    return await _build_state(
        db_session,
        plan,
        fundamentals_output=_fundamentals_output(),
        fundamentals_status="SUCCESS",
        sentiment_output=generic["sentiment_output"],
        sentiment_status="SUCCESS",
        risk_output=generic["risk_output"],
        risk_status="SUCCESS",
        macro_output=generic["macro_output"],
        macro_status="SUCCESS",
        comparables_output=generic["comparables_output"],
        comparables_status="SUCCESS",
    )


# ---------------------------------------------------------------------------
# _compute_memo_status unit tests (pure function, no DB)
# ---------------------------------------------------------------------------


def test_compute_memo_status_complete_all_six_success() -> None:
    """All 5 specialist SUCCESS + synthesis SUCCESS => COMPLETE."""
    assert (
        _compute_memo_status(["SUCCESS"] * 5, "SUCCESS") == "COMPLETE"
    )


def test_compute_memo_status_failed_all_six_failed() -> None:
    """All 5 specialist FAILED + synthesis FAILED => FAILED."""
    assert _compute_memo_status(["FAILED"] * 5, "FAILED") == "FAILED"


def test_compute_memo_status_partial_one_specialist_failed() -> None:
    """One specialist FAILED, rest SUCCESS, synthesis SUCCESS => PARTIAL."""
    statuses = ["SUCCESS", "SUCCESS", "FAILED", "SUCCESS", "SUCCESS"]
    assert _compute_memo_status(statuses, "SUCCESS") == "PARTIAL"


def test_compute_memo_status_partial_one_specialist_partial() -> None:
    """One specialist PARTIAL, rest SUCCESS, synthesis SUCCESS => PARTIAL."""
    statuses = ["SUCCESS", "PARTIAL", "SUCCESS", "SUCCESS", "SUCCESS"]
    assert _compute_memo_status(statuses, "SUCCESS") == "PARTIAL"


def test_compute_memo_status_partial_synthesis_failed_not_all_specialists_failed() -> (
    None
):
    """Synthesis FAILED but not every specialist FAILED => PARTIAL (never FAILED)."""
    statuses = ["SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS"]
    assert _compute_memo_status(statuses, "FAILED") == "PARTIAL"


# ---------------------------------------------------------------------------
# _build_prompt None-guard coverage
# ---------------------------------------------------------------------------


def test_build_prompt_embeds_all_five_none_guarded() -> None:
    """Prompt embeds a distinct block per specialist; missing ones are
    explicitly marked unavailable rather than omitted."""
    upstream = {
        "fundamentals_output": _fundamentals_output(),
        "sentiment_output": None,
        "risk_output": _specialist_output("risks", "Risk narrative here."),
        "macro_output": None,
        "comparables_output": None,
    }
    prompt = _build_prompt("AAPL", upstream)

    assert "FundamentalAnalysis findings" in prompt
    assert "8%" in prompt
    assert "RiskAssessment findings" in prompt
    assert "Risk narrative here." in prompt
    # None-guarded specialists are explicitly marked, never silently dropped.
    assert "SentimentNLP findings: unavailable for this run." in prompt
    assert "MacroSector findings: unavailable for this run." in prompt
    assert "ComparableCompanies findings: unavailable for this run." in prompt


# ---------------------------------------------------------------------------
# Synthesis reads fundamentals_output and produces a distinct take
# ---------------------------------------------------------------------------


async def test_synthesis_reads_fundamentals_output(db_session: AsyncSession) -> None:
    """Synthesis prompt references fundamentals findings; output has a take."""
    from app.agents.synthesis import synthesis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    fundamentals_output = _fundamentals_output()
    state = await _build_state(
        db_session,
        plan,
        fundamentals_output=fundamentals_output,
        fundamentals_status="SUCCESS",
    )

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
# Memo-status rule (D-02 ownership, generalized to 6 agents)
# ---------------------------------------------------------------------------


async def test_memo_status_complete_all_six_success(db_session: AsyncSession) -> None:
    """All 5 specialists SUCCESS + synthesis SUCCESS => memo_status COMPLETE."""
    from app.agents.synthesis import synthesis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = await _build_all_success_state(db_session, plan)

    with patch(
        "app.agents.synthesis.call_groq",
        AsyncMock(return_value="An overall take."),
    ):
        result = await synthesis_node(state)

    assert result["synthesis_status"] == AgentTaskStatus.SUCCESS.value
    assert result["memo_status"] == "COMPLETE"


async def test_memo_status_partial_on_one_specialist_failed(
    db_session: AsyncSession,
) -> None:
    """One specialist FAILED, rest SUCCESS, synthesis SUCCESS => PARTIAL (EXEC-04)."""
    from app.agents.synthesis import synthesis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    generic = _generic_outputs()
    state = await _build_state(
        db_session,
        plan,
        fundamentals_output=None,
        fundamentals_status="FAILED",
        sentiment_output=generic["sentiment_output"],
        sentiment_status="SUCCESS",
        risk_output=generic["risk_output"],
        risk_status="SUCCESS",
        macro_output=generic["macro_output"],
        macro_status="SUCCESS",
        comparables_output=generic["comparables_output"],
        comparables_status="SUCCESS",
    )

    with patch(
        "app.agents.synthesis.call_groq",
        AsyncMock(return_value="An overall take despite missing fundamentals."),
    ):
        result = await synthesis_node(state)

    assert result["synthesis_status"] == AgentTaskStatus.SUCCESS.value
    assert result["memo_status"] == "PARTIAL"


async def test_memo_status_partial_on_partial_specialist(
    db_session: AsyncSession,
) -> None:
    """One specialist PARTIAL, rest SUCCESS, synthesis SUCCESS => PARTIAL."""
    from app.agents.synthesis import synthesis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    generic = _generic_outputs()
    state = await _build_state(
        db_session,
        plan,
        fundamentals_output=_fundamentals_output(),
        fundamentals_status="PARTIAL",
        sentiment_output=generic["sentiment_output"],
        sentiment_status="SUCCESS",
        risk_output=generic["risk_output"],
        risk_status="SUCCESS",
        macro_output=generic["macro_output"],
        macro_status="SUCCESS",
        comparables_output=generic["comparables_output"],
        comparables_status="SUCCESS",
    )

    with patch(
        "app.agents.synthesis.call_groq",
        AsyncMock(return_value="An overall take."),
    ):
        result = await synthesis_node(state)

    assert result["synthesis_status"] == AgentTaskStatus.SUCCESS.value
    assert result["memo_status"] == "PARTIAL"


async def test_memo_status_failed_when_all_six_fail(db_session: AsyncSession) -> None:
    """All 5 specialists FAILED + synthesis FAILED => memo_status FAILED."""
    from app.agents.synthesis import synthesis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = await _build_state(
        db_session,
        plan,
        fundamentals_output=None,
        fundamentals_status="FAILED",
        sentiment_output=None,
        sentiment_status="FAILED",
        risk_output=None,
        risk_status="FAILED",
        macro_output=None,
        macro_status="FAILED",
        comparables_output=None,
        comparables_status="FAILED",
    )

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
    state = await _build_all_success_state(db_session, plan)

    with patch(
        "app.agents.synthesis.call_groq",
        AsyncMock(side_effect=RuntimeError("groq boom")),
    ):
        result = await synthesis_node(state)

    assert result["synthesis_status"] == "FAILED"
    assert result["synthesis_output"] is None
    # All 5 specialists SUCCESS but synthesis FAILED => PARTIAL, never masked
    # as COMPLETE and never silently promoted to FAILED (not all-specialist
    # FAILED).
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
    state = await _build_all_success_state(db_session, plan)

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
