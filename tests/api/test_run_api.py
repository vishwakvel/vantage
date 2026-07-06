"""Tests for POST /api/v1/research/{plan_id}/run (EXEC-05, 06-05-PLAN.md).

``run_plan`` no longer runs the research graph synchronously — it creates a
PENDING ``ResearchMemo`` row, dispatches ``run_research_task.delay(...)``,
and returns immediately (D-01/D-02/D-03). Coverage:
  - test_run_creates_pending_memo_and_dispatches_task: mocks
    ``run_research_task.delay``; asserts 200 with body
    ``{memo_id, plan_id, status: "PENDING"}`` (no per-agent fields, no
    task_id — D-03), exactly one ``ResearchMemo`` row is created with
    status PENDING and an empty body, and ``.delay`` was called exactly
    once with the created memo's id/plan_id/ticker/user_id.
  - test_run_other_user_plan_returns_404 / test_run_missing_plan_returns_404:
    IDOR — non-owned or missing plan_id returns 404, never 403; no memo is
    created and ``.delay`` is never called.
  - test_run_requires_auth: unauthenticated request rejected before any work;
    no memo created, ``.delay`` never called.
  - test_rerun_sets_parent_memo_id: running twice creates two ResearchMemo
    rows; the second's parent_memo_id equals the first's id (D-03 lineage
    is preserved by the dispatch-only endpoint too).

``run_research_task.delay`` is patched at the ``app.api.v1.research`` import
site (the same object ``app.workers.tasks.run_research_task`` — ``research.py``
imports it directly, so patching either path patches the same underlying
Celery task instance) — never touching a real broker/Redis.

NOTE ON RETAINED FIXTURES: ``_patch_all_agents``, ``_session_scope_targets_
test_db``, and the chunk/article/paper/observation/peer builder helpers
below are no longer exercised by this module's own tests (the endpoint they
supported — synchronous graph execution via HTTP — was removed in 06-05).
They are kept here because ``tests/graph/test_research_graph_integration.py``
imports them by name to drive the real compiled graph directly; moving them
would be a larger, out-of-scope refactor for this plan. See that module's
docstring for how it reuses them.

Reuses the seeding + authed/unauthed client helpers from
tests/api/test_research_api.py (D-03 db_session auto-skip when test-postgres
is unreachable).
"""

import uuid
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.db.session as db_session_module
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


@pytest.fixture(autouse=True)
async def _session_scope_targets_test_db(test_settings: Settings):
    """Point ``app.db.session``'s lazy engine/session-factory singleton at
    the test database for the duration of each test in this module.

    Retained for ``tests/graph/test_research_graph_integration.py``, which
    imports this fixture by name (autouse fixtures apply to any module they
    are imported into, per pytest's fixture-discovery-by-namespace
    mechanics) — see that module's docstring. This module's own tests no
    longer call the real ``session_scope()`` (the endpoint under test here
    only touches its own request-scoped session and mocks
    ``run_research_task.delay``), but leaving this fixture active is
    harmless — it only resets a module-level singleton.
    """
    original_engine = db_session_module._engine
    original_factory = db_session_module._session_factory
    db_session_module._engine = None
    db_session_module._session_factory = None

    with patch("app.db.session.get_settings", return_value=test_settings):
        yield

    if db_session_module._engine is not None:
        await db_session_module._engine.dispose()
    db_session_module._engine = original_engine
    db_session_module._session_factory = original_factory


_FUNDAMENTALS_NARRATIVE = "Comprehensive fundamentals narrative for AAPL."
_SENTIMENT_NARRATIVE = "Sentiment: bullish\n\nAAPL shows strong momentum."
_RISK_NARRATIVE = "Structured risk narrative for AAPL."
_MACRO_NARRATIVE = "Macro/sector narrative for AAPL."
_COMPARABLES_NARRATIVE = "Relative-valuation narrative for AAPL."
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


def _make_article() -> dict:
    return {
        "title": "AAPL beats earnings estimates",
        "description": "A description of the article.",
        "content": "Full article content.",
        "url": "https://example.com/article",
        "source": "Example News",
        "published_at": "2026-07-01T00:00:00Z",
    }


def _make_paper() -> dict:
    return {
        "title": "Deep learning for equity forecasting",
        "abstract": "An abstract discussing forecasting methods.",
        "url": "https://arxiv.org/abs/1234.5678",
        "published": "2026-06-30T00:00:00Z",
    }


def _make_fred_observation() -> dict:
    return {"value": 5.25, "date": "2026-06-01"}


def _make_peer_metric(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "market_cap": 2_000_000_000,
        "trailing_pe": 22.5,
        "profit_margin": 0.28,
        "revenue": 4_000_000_000,
    }


@contextmanager
def _patch_all_agents(
    *,
    chunks: list[dict] | None = None,
    news_articles: list[dict] | None = None,
    arxiv_papers: list[dict] | None = None,
    fred_observations: list[dict] | None = None,
    peers: list[str] | None = None,
    peer_metrics: list[dict] | None = None,
):
    """Patch every one of the 5 specialist + Synthesis agent modules'
    external service boundaries for a single real-graph invocation.
    ``session_scope()`` is deliberately left unpatched here — see the
    ``_session_scope_targets_test_db`` fixture docstring above.

    Retained for ``tests/graph/test_research_graph_integration.py`` (see
    module docstring) — no longer used by this module's own tests, since
    ``run_plan`` no longer invokes the graph synchronously.

    Defaults produce an all-SUCCESS run across every agent; pass an empty
    list for any one source to drive that agent's PARTIAL/FAILED path
    while the others still succeed (EXEC-04 coverage).
    """
    chunks = _full_coverage_chunks() if chunks is None else chunks
    news_articles = [_make_article()] if news_articles is None else news_articles
    arxiv_papers = [_make_paper()] if arxiv_papers is None else arxiv_papers
    fred_observations = (
        [_make_fred_observation()] if fred_observations is None else fred_observations
    )
    peers = ["MSFT", "GOOGL"] if peers is None else peers
    peer_metrics = (
        [_make_peer_metric(p) for p in peers] if peer_metrics is None else peer_metrics
    )

    patches = [
        # FundamentalAnalysis — reads state["session"].
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            new=AsyncMock(return_value=_FUNDAMENTALS_NARRATIVE),
        ),
        # SentimentNLP — own session via the real session_scope().
        patch(
            "app.agents.sentiment_nlp.news_client.get_recent_articles",
            new=AsyncMock(return_value=news_articles),
        ),
        patch(
            "app.agents.sentiment_nlp.arxiv_client.search",
            new=AsyncMock(return_value=arxiv_papers),
        ),
        patch(
            "app.agents.sentiment_nlp.call_groq",
            new=AsyncMock(return_value=_SENTIMENT_NARRATIVE),
        ),
        # RiskAssessment — own session via the real session_scope().
        patch("app.agents.risk_assessment.hybrid_retrieve", return_value=chunks),
        patch(
            "app.agents.risk_assessment.news_client.get_recent_articles",
            new=AsyncMock(return_value=news_articles),
        ),
        patch(
            "app.agents.risk_assessment.call_groq",
            new=AsyncMock(return_value=_RISK_NARRATIVE),
        ),
        # MacroSector — own session via the real session_scope().
        patch(
            "app.agents.macro_sector.fred_client.get_series_observations",
            new=AsyncMock(return_value=fred_observations),
        ),
        patch(
            "app.agents.macro_sector.call_groq",
            new=AsyncMock(return_value=_MACRO_NARRATIVE),
        ),
        # ComparableCompanies — own session via the real session_scope().
        patch(
            "app.agents.comparable_companies.comparables_source.get_peers",
            new=AsyncMock(return_value=peers),
        ),
        patch(
            "app.agents.comparable_companies.comparables_source.get_metrics",
            new=AsyncMock(return_value=peer_metrics),
        ),
        patch(
            "app.agents.comparable_companies.call_groq",
            new=AsyncMock(return_value=_COMPARABLES_NARRATIVE),
        ),
        # Synthesis — reads state["session"], no session_scope involved.
        patch(
            "app.agents.synthesis.call_groq",
            new=AsyncMock(return_value=_SYNTHESIS_TAKE),
        ),
    ]

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


# ---------------------------------------------------------------------------
# test_run_creates_pending_memo_and_dispatches_task (EXEC-05, D-01/D-02/D-03)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_creates_pending_memo_and_dispatches_task(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """POST /run returns immediately with a PENDING memo and dispatches the
    background task exactly once — no graph invocation, no per-agent
    fields, no task_id in the response (D-03)."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        with patch(
            "app.api.v1.research.run_research_task.delay", new=MagicMock()
        ) as mock_delay:
            resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"memo_id", "plan_id", "status"}
    assert body["plan_id"] == str(plan.id)
    assert body["status"] == "PENDING"

    memo_id = uuid.UUID(body["memo_id"])

    result = await db_session.execute(select(ResearchMemo))
    memos = result.scalars().all()
    assert len(memos) == 1
    memo = memos[0]
    assert memo.id == memo_id
    assert memo.status.value == "PENDING"
    assert memo.body == {}

    mock_delay.assert_called_once_with(
        memo_id=str(memo_id),
        plan_id=str(plan.id),
        ticker="AAPL",
        user_id=str(user.id),
    )


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

    async with _make_authed_client(db_session, test_settings, other_user) as client:
        with patch(
            "app.api.v1.research.run_research_task.delay", new=MagicMock()
        ) as mock_delay:
            resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")

    assert resp.status_code == 404, resp.text
    mock_delay.assert_not_called()

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

    async with _make_authed_client(db_session, test_settings, user) as client:
        with patch(
            "app.api.v1.research.run_research_task.delay", new=MagicMock()
        ) as mock_delay:
            resp = await client.post(f"{RESEARCH_URL}/{random_plan_id}/run")

    assert resp.status_code == 404, resp.text
    mock_delay.assert_not_called()


# ---------------------------------------------------------------------------
# test_run_requires_auth (T-06-07-AUTHZ)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_requires_auth(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Unauthenticated /run request is rejected before any work; no memo
    created, no dispatch."""
    user = await _seed_user(db_session)
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    async with _make_unauthed_client(db_session, test_settings) as client:
        with patch(
            "app.api.v1.research.run_research_task.delay", new=MagicMock()
        ) as mock_delay:
            resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")

    assert resp.status_code in (401, 403), (
        f"Expected 401 or 403 for unauthenticated /run, got "
        f"{resp.status_code}: {resp.text}"
    )
    mock_delay.assert_not_called()

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
    equals the first's id — lineage is preserved by the dispatch-only
    endpoint too."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        with patch("app.api.v1.research.run_research_task.delay", new=MagicMock()):
            first_resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")
        assert first_resp.status_code == 200, first_resp.text
        first_memo_id = uuid.UUID(first_resp.json()["memo_id"])

        with patch("app.api.v1.research.run_research_task.delay", new=MagicMock()):
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
