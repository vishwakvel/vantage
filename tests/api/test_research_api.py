"""Research API endpoint tests — POST /api/v1/research.

Coverage (03-01-PLAN.md, ROADMAP SC#1, REQST-01, REQST-02):
  - test_create_research_request_happy_path: an authenticated free-text
    request that resolves unambiguously returns 200, needs_clarification
    false, a valid plan_id, resolved_tickers == ["AAPL"], and a persisted
    ResearchPlan row exists in the DB.
  - test_create_research_request_requires_auth: an unauthenticated request
    returns 401/403 before any resolve/ingest work happens.

``ingestion_service.ingest_ticker`` is patched with an ``AsyncMock`` so no
real EDGAR/ChromaDB call happens (mirrors ``tests/test_ingest_api.py``
conventions — patch at the service boundary, never httpx/EDGAR directly).

Uses the ``async_client``-style pattern from ``tests/conftest.py`` (db_session
+ test_settings fixtures) plus a local ``get_current_user`` override (mirrors
``tests/test_ingest_api.py`` lines 79-96) so persistence assertions run
against a real (test-postgres) session while auth is short-circuited.

Skips automatically when test-postgres (port 5433) is unreachable, via the
``db_session`` fixture's built-in skip behavior (D-03).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.dependencies import get_current_user
from app.db.models import Company, ResearchPlan, User
from app.db.session import get_session
from app.main import create_app
from app.services.ingestion_service import IngestionResult

RESEARCH_URL = "/api/v1/research"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_user(db_session: AsyncSession) -> User:
    """Persist and return a User row (FK target for ResearchRequest/ResearchPlan)."""
    user = User(
        email=f"{uuid.uuid4()}@example.com",
        password_hash="not-a-real-hash",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


async def _seed_company(
    db_session: AsyncSession, ticker: str = "AAPL", name: str = "Apple Inc."
) -> Company:
    """Persist and return a Company row used by the fuzzy-match path."""
    company = Company(ticker=ticker, name=name)
    db_session.add(company)
    await db_session.flush()
    return company


def _make_authed_client(
    db_session: AsyncSession, test_settings: Settings, user: User
) -> AsyncClient:
    """Build an AsyncClient wired to db_session with get_current_user overridden.

    Mirrors the ``async_client`` fixture in conftest.py plus an
    authenticated-user override (mirrors ``tests/test_ingest_api.py``'s
    ``_make_app_with_auth_override``, lines 79-96) so the handler's
    ``user.id`` references a real, persisted ``User`` row (satisfying the
    ``research_requests.user_id`` / ``research_plans.user_id`` FK constraints).
    """
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: test_settings

    async def _override_session():
        yield db_session

    application.dependency_overrides[get_session] = _override_session
    application.dependency_overrides[get_current_user] = lambda: user

    return AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    )


def _make_unauthed_client(
    db_session: AsyncSession, test_settings: Settings
) -> AsyncClient:
    """Build an AsyncClient with NO get_current_user override (requires real JWT)."""
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: test_settings

    async def _override_session():
        yield db_session

    application.dependency_overrides[get_session] = _override_session

    return AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    )


# ---------------------------------------------------------------------------
# test_create_research_request_happy_path (SC#1, REQST-01, REQST-02)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_research_request_happy_path(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Authenticated POST /api/v1/research resolves AAPL and persists a ResearchPlan."""
    user = await _seed_user(db_session)
    await _seed_company(db_session)
    await db_session.commit()

    mock_result = IngestionResult(
        ticker="AAPL", filings_ingested=0, filings_cached=0, source_warnings=[]
    )

    async with _make_authed_client(db_session, test_settings, user) as client:
        with patch(
            "app.api.v1.research.ingestion_service.ingest_ticker",
            new=AsyncMock(return_value=mock_result),
        ) as mock_ingest:
            resp = await client.post(
                RESEARCH_URL, json={"raw_query": "Tell me about Apple"}
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["needs_clarification"] is False
    assert body["resolved_tickers"] == ["AAPL"]
    plan_id = uuid.UUID(body["plan_id"])  # valid UUID string
    mock_ingest.assert_awaited_once()

    result = await db_session.execute(
        select(ResearchPlan).where(ResearchPlan.id == plan_id)
    )
    plan = result.scalar_one_or_none()
    assert plan is not None
    assert plan.resolved_tickers == ["AAPL"]


# ---------------------------------------------------------------------------
# test_create_research_request_requires_auth (T-03-04, mirrors WR-03)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_research_request_requires_auth(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Unauthenticated POST /api/v1/research returns 401 or 403.

    FastAPI's HTTPBearer dependency raises 403 (not 401) by default when the
    Authorization header is missing — both are accepted as "unauthenticated".
    """
    with patch(
        "app.api.v1.research.ingestion_service.ingest_ticker",
        new=AsyncMock(),
    ) as mock_ingest:
        async with _make_unauthed_client(db_session, test_settings) as client:
            resp = await client.post(
                RESEARCH_URL, json={"raw_query": "Tell me about Apple"}
            )

    assert resp.status_code in (401, 403), (
        f"Expected 401 or 403 for unauthenticated research request, "
        f"got {resp.status_code}: {resp.text}"
    )
    mock_ingest.assert_not_awaited()
