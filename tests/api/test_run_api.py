"""End-to-end tests for POST /api/v1/research/{plan_id}/run.

Coverage (05-10-PLAN.md, generalizing 04-05-PLAN.md's 2-agent tests to the
full 5-way parallel fan-out; EXEC-02, EXEC-04, MEMO-01, D-03, T-04-IDOR,
AGENT-05, AGENT-06):
  - test_run_happy_path_has_named_sections: full coverage across all 5
    specialists yields a persisted ResearchMemo with a named section for
    every one of the 5 specialists plus synthesis (MEMO-01).
  - test_run_reports_per_agent_statuses: response body reports every one of
    the 6 agents' statuses among SUCCESS/PARTIAL/FAILED plus the overall
    memo status (EXEC-02).
  - test_run_partial_on_fundamentals_failure: zero retrieved chunks ->
    200 (no 5xx), persisted ResearchMemo.status == "PARTIAL",
    fundamentals agent status == "FAILED" (EXEC-03, SC#5).
  - test_run_partial_on_one_specialist_failure: one specialist's source
    returns empty (comparables get_peers -> []) while the other 4 succeed ->
    200, memo PARTIAL, comparables section carries a non-null reason
    sourced from AgentOutput.missing_fields (EXEC-04).
  - test_run_other_user_plan_returns_404 / test_run_missing_plan_returns_404:
    IDOR — non-owned or missing plan_id returns 404, never 403.
  - test_run_requires_auth: unauthenticated request rejected before any work.
  - test_rerun_sets_parent_memo_id: running twice creates two ResearchMemo
    rows; the second's parent_memo_id equals the first's id (D-03).
  - test_run_citations_present_in_fundamentals: fundamentals section
    citations each carry a canonical_id and a non-empty quote
    (MEMO-02/MEMO-03 e2e).

Patches target the SERVICE boundary only — ``call_groq``, ``hybrid_retrieve``,
``news_client``, ``arxiv_client``, ``fred_client``, and ``comparables_source``
in each of the 6 agent modules — never the groq SDK, httpx, or any 3rd-party
client directly (mirrors ``tests/agents/test_fundamental_analysis.py`` and
``tests/agents/test_synthesis.py`` conventions).

The 4 new specialist agents open their OWN ``AsyncSession`` via
``session_scope()`` and LangGraph genuinely dispatches them CONCURRENTLY
through the real compiled graph in these end-to-end tests — so, unlike the
per-agent unit tests, ``session_scope()`` is deliberately left UNPATCHED
here. Patching all 4 concurrent specialists to share one ``AsyncSession``
would reintroduce exactly the "another operation is in progress" collision
``session_scope()`` exists to prevent (05-01-PLAN.md). Instead, the
``_session_scope_targets_test_db`` autouse fixture below resets
``app.db.session``'s lazy engine/session-factory singleton and points its
``get_settings`` at ``test_settings``, so every real, independent
``session_scope()`` call — from any of the 4 concurrently-dispatched
specialists — opens its own connection against the SAME test-postgres
database the ``db_session`` fixture already created the schema in.

Reuses the seeding + authed/unauthed client helpers from
tests/api/test_research_api.py (D-03 db_session auto-skip when test-postgres
is unreachable).
"""

import uuid
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, patch

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

    The 4 new specialist agents call the REAL ``session_scope()`` directly
    (not through FastAPI's ``get_session`` DI, which only the request's own
    session uses) — without this, the first concurrent specialist to build
    the lazy singleton would read the real process environment/``.env``
    ``Settings`` (missing ``DATABASE_URL``/``JWT_SECRET_KEY``/``GROQ_API_KEY``
    in this test process, or worse, a real dev database if a `.env` happens
    to be present). Resetting the singleton and patching ``get_settings``
    makes every ``session_scope()`` call build a fresh connection against
    ``test_settings.DATABASE_URL`` instead — the same test-postgres instance
    ``db_session`` already created the schema in.
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
    external service boundaries for a single /run invocation, so the real
    compiled 5-way fan-out graph runs hermetically (no live network calls).
    ``session_scope()`` is deliberately left unpatched here — see the
    ``_session_scope_targets_test_db`` fixture docstring above.

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
# test_run_happy_path_has_named_sections (MEMO-01)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_happy_path_has_named_sections(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """POST /run with full coverage persists a memo with every named section."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        with _patch_all_agents():
            resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    memo_id = uuid.UUID(body["memo_id"])

    result = await db_session.execute(
        select(ResearchMemo).where(ResearchMemo.id == memo_id)
    )
    memo = result.scalar_one_or_none()
    assert memo is not None
    for section in (
        "fundamentals",
        "sentiment",
        "risks",
        "macro",
        "comparables",
        "synthesis",
    ):
        assert section in memo.body, f"missing section {section!r} in memo.body"
        assert memo.body[section] is not None


# ---------------------------------------------------------------------------
# test_run_reports_per_agent_statuses (EXEC-02)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_reports_per_agent_statuses(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Response body reports every agent's status and the overall memo status."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        with _patch_all_agents():
            resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    for field in (
        "fundamentals_status",
        "sentiment_status",
        "risk_status",
        "macro_status",
        "comparables_status",
        "synthesis_status",
    ):
        assert body[field] in ("SUCCESS", "PARTIAL", "FAILED"), field
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

    async with _make_authed_client(db_session, test_settings, user) as client:
        with _patch_all_agents(chunks=[]):
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
# test_run_partial_on_one_specialist_failure (EXEC-04)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_partial_on_one_specialist_failure(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Comparables source returns no peers while the other 4 agents succeed
    -> 200, memo PARTIAL, comparables section carries a non-null reason
    string (EXEC-04: never silently omitted)."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        with _patch_all_agents(peers=[]):
            resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["comparables_status"] == "FAILED"
    assert body["status"] == "PARTIAL"

    memo_id = uuid.UUID(body["memo_id"])
    result = await db_session.execute(
        select(ResearchMemo).where(ResearchMemo.id == memo_id)
    )
    memo = result.scalar_one_or_none()
    assert memo is not None
    comparables_section = memo.body["comparables"]
    assert comparables_section is not None
    assert comparables_section.get("reason")


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
        with _patch_all_agents():
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

    async with _make_authed_client(db_session, test_settings, user) as client:
        with _patch_all_agents():
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

    async with _make_unauthed_client(db_session, test_settings) as client:
        with _patch_all_agents():
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
        with _patch_all_agents():
            first_resp = await client.post(f"{RESEARCH_URL}/{plan.id}/run")
        assert first_resp.status_code == 200, first_resp.text
        first_memo_id = uuid.UUID(first_resp.json()["memo_id"])

        with _patch_all_agents():
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

    async with _make_authed_client(db_session, test_settings, user) as client:
        with _patch_all_agents():
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
