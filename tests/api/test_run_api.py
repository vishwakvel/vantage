"""End-to-end tests for POST /api/v1/research/{plan_id}/run.

Coverage (04-05-PLAN.md, EXEC-02, EXEC-03, MEMO-01, D-03, T-04-IDOR):
  - test_run_happy_path_has_named_sections: full section coverage yields a
    persisted ResearchMemo with a named "fundamentals" and "synthesis"
    section (MEMO-01).
  - test_run_reports_per_agent_statuses: response body reports each agent's
    status among SUCCESS/PARTIAL/FAILED plus the overall memo status
    (EXEC-02).
  - test_run_partial_on_fundamentals_failure: zero retrieved chunks ->
    200 (no 5xx), persisted ResearchMemo.status == "PARTIAL",
    fundamentals agent status == "FAILED" (EXEC-03, SC#5).
  - test_run_other_user_plan_returns_404 / test_run_missing_plan_returns_404:
    IDOR — non-owned or missing plan_id returns 404, never 403.
  - test_run_requires_auth: unauthenticated request rejected before any work.
  - test_rerun_sets_parent_memo_id: running twice creates two ResearchMemo
    rows; the second's parent_memo_id equals the first's id (D-03).
  - test_run_citations_present_in_fundamentals: fundamentals section
    citations each carry a canonical_id and a non-empty quote
    (MEMO-02/MEMO-03 e2e).

Patches target the SERVICE boundary only — ``app.agents.fundamental_analysis
.call_groq``, ``app.agents.fundamental_analysis.hybrid_retrieve``, and
``app.agents.synthesis.call_groq`` — never the groq SDK or ChromaDB directly
(mirrors tests/agents/test_fundamental_analysis.py and
tests/agents/test_synthesis.py conventions).

Reuses the seeding + authed/unauthed client helpers from
tests/api/test_research_api.py (D-03 db_session auto-skip when test-postgres
is unreachable).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ResearchMemo
from app.ingestion.section_constants import (
    SECTION_FINANCIALS,
    SECTION_MDA,
    SECTION_NOTES,
    SECTION_RISK_FACTORS,
)
from tests.api.test_research_api import (
    RESEARCH_URL,
    _make_authed_client,
    _make_unauthed_client,
    _seed_company,
    _seed_research_plan,
    _seed_user,
)

_FUNDAMENTALS_NARRATIVE = "Comprehensive fundamentals narrative for AAPL."
_SYNTHESIS_TAKE = "Distinct overall investment take on AAPL."


def _make_chunk(section: str, idx: int) -> dict:
    """Build a fake hybrid_retrieve chunk for the given section."""
    return {
        "id": f"chunk-{section}-{idx}",
        "text": f"Sample {section} excerpt {idx} with financial detail.",
        "metadata": {
            "canonical_id": f"canon-{section}-{idx}",
            "section": section,
            "form_type": "10-K",
            "period_of_report": "2023-09-30",
        },
    }


def _full_coverage_chunks() -> list[dict]:
    """One chunk per target section — triggers SUCCESS/FULL coverage."""
    return [
        _make_chunk(SECTION_MDA, 1),
        _make_chunk(SECTION_FINANCIALS, 2),
        _make_chunk(SECTION_NOTES, 3),
        _make_chunk(SECTION_RISK_FACTORS, 4),
    ]


def _patch_agents(chunks: list[dict]):
    """Patch the agent-module service boundary for a single /run invocation.

    Returns a combined context manager patching:
    - hybrid_retrieve (sync call in fundamental_analysis_node) -> chunks
    - fundamental_analysis.call_groq (async) -> narrative
    - synthesis.call_groq (async) -> take
    """
    return (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve",
            return_value=chunks,
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            new=AsyncMock(return_value=_FUNDAMENTALS_NARRATIVE),
        ),
        patch(
            "app.agents.synthesis.call_groq",
            new=AsyncMock(return_value=_SYNTHESIS_TAKE),
        ),
    )


# ---------------------------------------------------------------------------
# test_run_happy_path_has_named_sections (MEMO-01)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_happy_path_has_named_sections(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """POST /run with full section coverage persists a memo with named sections."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    p_retrieve, p_fund_llm, p_synth_llm = _patch_agents(_full_coverage_chunks())

    async with _make_authed_client(db_session, test_settings, user) as client:
        with p_retrieve, p_fund_llm, p_synth_llm:
            resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    memo_id = uuid.UUID(body["memo_id"])

    result = await db_session.execute(
        select(ResearchMemo).where(ResearchMemo.id == memo_id)
    )
    memo = result.scalar_one_or_none()
    assert memo is not None
    assert "fundamentals" in memo.body
    assert "synthesis" in memo.body
    assert memo.body["fundamentals"] is not None
    assert memo.body["synthesis"] is not None


# ---------------------------------------------------------------------------
# test_run_reports_per_agent_statuses (EXEC-02)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_reports_per_agent_statuses(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Response body reports per-agent statuses and the overall memo status."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    p_retrieve, p_fund_llm, p_synth_llm = _patch_agents(_full_coverage_chunks())

    async with _make_authed_client(db_session, test_settings, user) as client:
        with p_retrieve, p_fund_llm, p_synth_llm:
            resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fundamentals_status"] in ("SUCCESS", "PARTIAL", "FAILED")
    assert body["synthesis_status"] in ("SUCCESS", "PARTIAL", "FAILED")
    assert body["status"] in ("COMPLETE", "PARTIAL", "FAILED")


# ---------------------------------------------------------------------------
# test_run_partial_on_fundamentals_failure (EXEC-03, SC#5)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_partial_on_fundamentals_failure(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Zero retrieved chunks -> 200, memo PARTIAL, fundamentals agent FAILED."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    p_retrieve, p_fund_llm, p_synth_llm = _patch_agents([])

    async with _make_authed_client(db_session, test_settings, user) as client:
        with p_retrieve, p_fund_llm, p_synth_llm:
            resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fundamentals_status"] == "FAILED"
    assert body["status"] == "PARTIAL"

    memo_id = uuid.UUID(body["memo_id"])
    result = await db_session.execute(
        select(ResearchMemo).where(ResearchMemo.id == memo_id)
    )
    memo = result.scalar_one_or_none()
    assert memo is not None
    assert memo.status.value == "PARTIAL"


# ---------------------------------------------------------------------------
# IDOR — test_run_other_user_plan_returns_404 / test_run_missing_plan_returns_404
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_other_user_plan_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A plan owned by a DIFFERENT user returns 404, never 403 (IDOR / OWASP A01)."""
    owner = await _seed_user(db_session)
    other_user = await _seed_user(db_session)
    plan = await _seed_research_plan(db_session, owner, resolved_tickers=["AAPL"])
    await db_session.commit()

    p_retrieve, p_fund_llm, p_synth_llm = _patch_agents(_full_coverage_chunks())

    async with _make_authed_client(db_session, test_settings, other_user) as client:
        with p_retrieve, p_fund_llm, p_synth_llm:
            resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")

    assert resp.status_code == 404, resp.text

    memo_rows = await db_session.execute(select(ResearchMemo))
    assert memo_rows.scalars().all() == []


@pytest.mark.anyio
async def test_run_missing_plan_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A random, non-existent plan_id returns 404."""
    user = await _seed_user(db_session)
    await db_session.commit()

    random_plan_id = uuid.uuid4()

    p_retrieve, p_fund_llm, p_synth_llm = _patch_agents(_full_coverage_chunks())

    async with _make_authed_client(db_session, test_settings, user) as client:
        with p_retrieve, p_fund_llm, p_synth_llm:
            resp = await client.post(f"{RESEARCH_URL}/{random_plan_id}/run")

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# test_run_requires_auth (T-04-AUTHZ)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_requires_auth(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Unauthenticated /run request is rejected before any work; no memo created."""
    user = await _seed_user(db_session)
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    p_retrieve, p_fund_llm, p_synth_llm = _patch_agents(_full_coverage_chunks())

    async with _make_unauthed_client(db_session, test_settings) as client:
        with p_retrieve, p_fund_llm, p_synth_llm:
            resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")

    assert resp.status_code in (401, 403), (
        f"Expected 401 or 403 for unauthenticated /run, got "
        f"{resp.status_code}: {resp.text}"
    )

    memo_rows = await db_session.execute(select(ResearchMemo))
    assert memo_rows.scalars().all() == []


# ---------------------------------------------------------------------------
# test_rerun_sets_parent_memo_id (D-03)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rerun_sets_parent_memo_id(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Running twice creates two ResearchMemo rows; the second's parent_memo_id
    equals the first's id."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        p1_retrieve, p1_fund_llm, p1_synth_llm = _patch_agents(
            _full_coverage_chunks()
        )
        with p1_retrieve, p1_fund_llm, p1_synth_llm:
            first_resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")
        assert first_resp.status_code == 200, first_resp.text
        first_memo_id = uuid.UUID(first_resp.json()["memo_id"])

        p2_retrieve, p2_fund_llm, p2_synth_llm = _patch_agents(
            _full_coverage_chunks()
        )
        with p2_retrieve, p2_fund_llm, p2_synth_llm:
            second_resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")
        assert second_resp.status_code == 200, second_resp.text
        second_memo_id = uuid.UUID(second_resp.json()["memo_id"])

    assert first_memo_id != second_memo_id

    memo_rows = await db_session.execute(
        select(ResearchMemo).where(ResearchMemo.plan_id == plan.id)
    )
    memos = memo_rows.scalars().all()
    assert len(memos) == 2

    second_memo = next(m for m in memos if m.id == second_memo_id)
    assert second_memo.parent_memo_id == first_memo_id


# ---------------------------------------------------------------------------
# test_run_citations_present_in_fundamentals (MEMO-02/MEMO-03 e2e)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_citations_present_in_fundamentals(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Fundamentals section citations each carry a canonical_id and non-empty quote."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    p_retrieve, p_fund_llm, p_synth_llm = _patch_agents(_full_coverage_chunks())

    async with _make_authed_client(db_session, test_settings, user) as client:
        with p_retrieve, p_fund_llm, p_synth_llm:
            resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")

    assert resp.status_code == 200, resp.text
    memo_id = uuid.UUID(resp.json()["memo_id"])

    result = await db_session.execute(
        select(ResearchMemo).where(ResearchMemo.id == memo_id)
    )
    memo = result.scalar_one_or_none()
    assert memo is not None

    citations = memo.body["fundamentals"]["citations"]
    assert len(citations) > 0
    for citation in citations:
        assert citation["canonical_id"]
        assert citation["quote"]
