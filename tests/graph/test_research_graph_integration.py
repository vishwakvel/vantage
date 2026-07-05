"""Integration test for the REAL compiled 5-way fan-out + fan-in research
graph (05-10-PLAN.md).

Unlike ``tests/graph/test_research_graph.py`` (structural/compile-only) and
``tests/api/test_run_api.py`` (through the HTTP endpoint), this exercises
``build_research_graph().ainvoke(...)`` directly against the real compiled
graph to prove:
  - AGENT-05/AGENT-06: a single run dispatches all 5 specialists concurrently
    and Synthesis's fan-in waits for all of them — proven by one AgentTask
    row per agent_type (6 total) persisting from one ``ainvoke`` call.
  - All-SUCCESS -> ``final_state["memo_status"] == "COMPLETE"``.
  - EXEC-04: one specialist's source returning empty (comparables
    ``get_peers`` -> ``[]``) while the other 4 succeed yields that agent
    FAILED, ``memo_status`` PARTIAL, and — replicating the exact
    section-assembly logic ``app.api.v1.research.run_plan`` uses — the
    comparables section's key is never omitted and carries a non-null
    user-facing reason sourced from ``AgentOutput.missing_fields``.

Reuses ``tests/api/test_run_api.py``'s ``_patch_all_agents`` (external
service-boundary patches for all 6 agent modules) and
``_session_scope_targets_test_db`` (resets ``app.db.session``'s lazy
engine/session-factory singleton and points ``get_settings`` at
``test_settings``) rather than re-deriving the same concurrency-safety
mechanics — session_scope() is deliberately left UNPATCHED here too, since
the 4 new specialists run genuinely concurrently through the real graph and
sharing one ``AsyncSession`` across them would reintroduce the exact
"another operation is in progress" collision ``session_scope()`` exists to
prevent (05-01-PLAN.md). No real network calls; specialist sessions target
test-postgres via the fixture, never the dev DB.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.research import (
    _AGENT_TYPE_BY_SECTION,
    _SECTION_STATE_FIELDS,
    _extract_reason,
)
from app.db.models import AgentOutput, AgentTask, ResearchPlan, ResearchRequest, User
from app.graph.research_graph import build_research_graph
from app.ingestion.section_constants import SECTION_COMPARABLES
from tests.api.test_run_api import (  # noqa: F401 — fixture imported for autouse
    _patch_all_agents,
    _session_scope_targets_test_db,
)

pytestmark = pytest.mark.anyio

#: The 6 AgentTask.agent_type values one all-data run must persist —
#: proves concurrent dispatch of all 5 specialists (AGENT-05) plus the
#: Synthesis fan-in (AGENT-06).
_ALL_AGENT_TYPES: frozenset[str] = frozenset(
    {
        "FundamentalAnalysis",
        "SentimentNLP",
        "RiskAssessment",
        "MacroSector",
        "ComparableCompanies",
        "Synthesis",
    }
)


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


def _build_initial_state(db_session: AsyncSession, plan: ResearchPlan, user: User) -> dict:
    """Mirrors app.api.v1.research.run_plan's initial_state construction."""
    return {
        "plan_id": str(plan.id),
        "ticker": "AAPL",
        "user_id": str(user.id),
        "session": db_session,
        "fundamentals_output": None,
        "fundamentals_status": "",
        "sentiment_output": None,
        "sentiment_status": "",
        "risk_output": None,
        "risk_status": "",
        "macro_output": None,
        "macro_status": "",
        "comparables_output": None,
        "comparables_status": "",
        "synthesis_output": None,
        "synthesis_status": "",
        "memo_status": "",
    }


async def _assemble_memo_body(db_session: AsyncSession, plan: ResearchPlan, final_state: dict) -> dict:
    """Replicates app.api.v1.research.run_plan's memo body assembly exactly,
    so this graph-level integration test can assert on the SAME EXEC-04
    section-never-omitted / reason-sourcing behavior the endpoint persists,
    without going through the HTTP layer.
    """
    reason_result = await db_session.execute(
        select(AgentTask.agent_type, AgentTask.created_at, AgentOutput.missing_fields)
        .join(AgentOutput, AgentOutput.task_id == AgentTask.id)
        .where(
            AgentTask.plan_id == plan.id,
            AgentTask.agent_type.in_(_AGENT_TYPE_BY_SECTION.values()),
        )
        .order_by(AgentTask.created_at.desc())
    )
    reasons_by_agent_type: dict[str, str | None] = {}
    for agent_type, _created_at, missing_fields in reason_result.all():
        if agent_type not in reasons_by_agent_type:
            reasons_by_agent_type[agent_type] = _extract_reason(missing_fields)

    body: dict = {}
    for section, (output_field, status_field) in _SECTION_STATE_FIELDS.items():
        output = final_state.get(output_field)
        if output is not None:
            body[section] = output
        else:
            agent_type = _AGENT_TYPE_BY_SECTION[section]
            body[section] = {
                "narrative": None,
                "status": final_state.get(status_field),
                "reason": reasons_by_agent_type.get(agent_type),
            }
    return body


# ---------------------------------------------------------------------------
# AGENT-05/AGENT-06 — concurrent dispatch + fan-in over the real graph
# ---------------------------------------------------------------------------


async def test_all_agent_types_persist_one_task_per_run(
    db_session: AsyncSession,
) -> None:
    """One ainvoke() call dispatches all 5 specialists concurrently and
    Synthesis's fan-in waits on all of them — proven by exactly one
    AgentTask row per agent_type (6 total) after a single run."""
    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_initial_state(db_session, plan, user)

    with _patch_all_agents():
        final_state = await build_research_graph().ainvoke(state)

    for field in (
        "fundamentals_status",
        "sentiment_status",
        "risk_status",
        "macro_status",
        "comparables_status",
        "synthesis_status",
    ):
        assert final_state.get(field), f"{field} missing/empty in final_state"

    task_rows = (
        (
            await db_session.execute(
                select(AgentTask).where(AgentTask.plan_id == plan.id)
            )
        )
        .scalars()
        .all()
    )
    persisted_agent_types = {task.agent_type for task in task_rows}
    assert persisted_agent_types == _ALL_AGENT_TYPES
    assert len(task_rows) == 6, "expected exactly one AgentTask per agent_type"


# ---------------------------------------------------------------------------
# All-SUCCESS -> COMPLETE
# ---------------------------------------------------------------------------


async def test_all_success_yields_complete_memo_status(
    db_session: AsyncSession,
) -> None:
    """Every specialist + synthesis SUCCESS -> memo_status COMPLETE."""
    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_initial_state(db_session, plan, user)

    with _patch_all_agents():
        final_state = await build_research_graph().ainvoke(state)

    for field in (
        "fundamentals_status",
        "sentiment_status",
        "risk_status",
        "macro_status",
        "comparables_status",
        "synthesis_status",
    ):
        assert final_state[field] == "SUCCESS", f"{field} was {final_state[field]!r}"

    assert final_state["memo_status"] == "COMPLETE"


# ---------------------------------------------------------------------------
# EXEC-04 — one specialist FAILED -> PARTIAL, section never omitted, reason present
# ---------------------------------------------------------------------------


async def test_one_agent_failure_yields_partial_with_reason(
    db_session: AsyncSession,
) -> None:
    """Comparables source returns no peers while the other 4 specialists
    succeed -> comparables agent FAILED, memo_status PARTIAL, and the
    assembled memo body's comparables section carries a non-null reason
    (section key NOT omitted)."""
    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = _build_initial_state(db_session, plan, user)

    with _patch_all_agents(peers=[]):
        final_state = await build_research_graph().ainvoke(state)

    assert final_state["comparables_status"] == "FAILED"
    assert final_state["comparables_output"] is None
    assert final_state["memo_status"] == "PARTIAL"

    # The other 4 specialists + synthesis still succeeded — only the one
    # forced-empty source degraded.
    for field in (
        "fundamentals_status",
        "sentiment_status",
        "risk_status",
        "macro_status",
        "synthesis_status",
    ):
        assert final_state[field] == "SUCCESS", f"{field} was {final_state[field]!r}"

    body = await _assemble_memo_body(db_session, plan, final_state)

    # EXEC-04: every section key present, even the failed one.
    for section in _SECTION_STATE_FIELDS:
        assert section in body, f"section {section!r} silently omitted"

    comparables_section = body[SECTION_COMPARABLES]
    assert comparables_section is not None
    assert comparables_section["reason"], "failed section must carry a non-null reason"
    assert comparables_section["status"] == "FAILED"

    # The 4 succeeding sections still carry their real (non-marker) output.
    assert "reason" not in body["fundamentals"]
    assert "reason" not in body["sentiment"]
    assert "reason" not in body["risks"]
    assert "reason" not in body["macro"]
    assert "reason" not in body["synthesis"]
