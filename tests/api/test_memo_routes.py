"""Tests for the D-04 memo read paths (06-05-PLAN.md, EXEC-05).

Coverage:
  - test_get_memo_by_id_returns_owner_memo: GET /memo/{id} for its owner
    returns 200 with the memo's id/plan_id/status/ticker/body.
  - test_get_memo_by_id_other_user_returns_404: GET /memo/{id} for a memo
    owned by a DIFFERENT user returns 404 (never 403 — IDOR).
  - test_get_memo_by_id_unknown_returns_404: GET /memo/{random-uuid} returns
    404.
  - test_get_latest_memo_for_plan_returns_newest: GET /{plan_id}/memo
    returns the newest memo by created_at when several exist for the plan.
  - test_get_latest_memo_for_plan_no_memo_returns_404: GET /{plan_id}/memo
    with no memo yet returns 404.
  - test_get_latest_memo_for_plan_other_user_returns_404: GET /{plan_id}/memo
    for a plan owned by a DIFFERENT user returns 404 (IDOR).
  - test_memo_routes_require_auth: both routes reject unauthenticated
    requests with 401/403 before any DB work.

Reuses the seeding + authed/unauthed client helpers from
tests/api/test_research_api.py (D-03 db_session auto-skip when test-postgres
is unreachable), mirroring tests/api/test_run_api.py's import pattern.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ResearchMemo, ResearchMemoStatus, ResearchPlan, User
from tests.api.test_research_api import (
    RESEARCH_URL,
    _make_authed_client,
    _make_unauthed_client,
    _seed_company,
    _seed_research_plan,
    _seed_user,
)


async def _seed_memo(
    db_session: AsyncSession,
    plan: ResearchPlan,
    owner: User,
    *,
    ticker: str | None = "AAPL",
    status: ResearchMemoStatus = ResearchMemoStatus.COMPLETE,
    body: dict | None = None,
) -> ResearchMemo:
    """Persist a ResearchMemo directly (no HTTP dispatch) and commit in its
    own transaction, so ``created_at`` (Postgres ``now()``, fixed per
    transaction) differs across sequential calls — required for the
    latest-by-plan ordering assertions below.
    """
    memo = ResearchMemo(
        plan_id=plan.id,
        user_id=owner.id,
        ticker=ticker,
        status=status,
        body=body if body is not None else {"synthesis": {"narrative": "take"}},
    )
    db_session.add(memo)
    await db_session.commit()
    await db_session.refresh(memo)
    return memo


# ---------------------------------------------------------------------------
# GET /research/memo/{memo_id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_memo_by_id_returns_owner_memo(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """GET /memo/{id} for its owner returns 200 with the full memo."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, user)

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.get(f"{RESEARCH_URL}/memo/{memo.id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["memo_id"] == str(memo.id)
    assert body["plan_id"] == str(plan.id)
    assert body["status"] == "COMPLETE"
    assert body["ticker"] == "AAPL"
    assert body["body"] == {"synthesis": {"narrative": "take"}}


@pytest.mark.anyio
async def test_get_memo_by_id_other_user_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A memo owned by a DIFFERENT user returns 404, never 403 (IDOR)."""
    owner = await _seed_user(db_session)
    other_user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, owner, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, owner)

    async with _make_authed_client(db_session, test_settings, other_user) as client:
        resp = await client.get(f"{RESEARCH_URL}/memo/{memo.id}")

    assert resp.status_code == 404, resp.text


@pytest.mark.anyio
async def test_get_memo_by_id_unknown_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A random, non-existent memo_id returns 404."""
    user = await _seed_user(db_session)
    await db_session.commit()

    random_memo_id = uuid.uuid4()

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.get(f"{RESEARCH_URL}/memo/{random_memo_id}")

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# GET /research/{plan_id}/memo
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_latest_memo_for_plan_returns_newest(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """When several memos exist for a plan, the newest by created_at wins."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    first = await _seed_memo(
        db_session, plan, user, status=ResearchMemoStatus.PARTIAL,
        body={"synthesis": {"narrative": "first"}},
    )
    second = await _seed_memo(
        db_session, plan, user, status=ResearchMemoStatus.COMPLETE,
        body={"synthesis": {"narrative": "second"}},
    )
    assert second.created_at >= first.created_at

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.get(f"{RESEARCH_URL}/{plan.id}/memo")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["memo_id"] == str(second.id)
    assert body["status"] == "COMPLETE"
    assert body["body"] == {"synthesis": {"narrative": "second"}}


@pytest.mark.anyio
async def test_get_latest_memo_for_plan_no_memo_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A plan with no memo yet returns 404."""
    user = await _seed_user(db_session)
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.get(f"{RESEARCH_URL}/{plan.id}/memo")

    assert resp.status_code == 404, resp.text


@pytest.mark.anyio
async def test_get_latest_memo_for_plan_other_user_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A plan owned by a DIFFERENT user returns 404, never 403 (IDOR)."""
    owner = await _seed_user(db_session)
    other_user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, owner, resolved_tickers=["AAPL"])
    await db_session.commit()
    await _seed_memo(db_session, plan, owner)

    async with _make_authed_client(db_session, test_settings, other_user) as client:
        resp = await client.get(f"{RESEARCH_URL}/{plan.id}/memo")

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Auth required on both routes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_memo_routes_require_auth(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Both GET memo routes reject unauthenticated requests before any DB
    work (401/403)."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(db_session, plan, user)

    async with _make_unauthed_client(db_session, test_settings) as client:
        memo_resp = await client.get(f"{RESEARCH_URL}/memo/{memo.id}")
        plan_resp = await client.get(f"{RESEARCH_URL}/{plan.id}/memo")

    assert memo_resp.status_code in (401, 403), memo_resp.text
    assert plan_resp.status_code in (401, 403), plan_resp.text
