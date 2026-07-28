"""Tests for ``app.services.watchlist_service`` (10-03-PLAN.md).

Coverage:

Group A — ``validate_rule_config``, DB-free (no ``db_session`` fixture, so
these run and can fail in any environment, including one with no
PostgreSQL container):
  - accepts an empty NEW_FILING config and returns {} (D-12)
  - rejects a NEW_FILING config carrying any key (D-12)
  - accepts a valid PRICE_MOVE config and normalises an int threshold_pct
    to float (D-09)
  - accepts each of up/down/either as a direction
  - rejects an unknown direction
  - rejects a missing direction key and a missing threshold_pct key
  - rejects an extra unrecognised key alongside otherwise-valid PRICE_MOVE
    fields
  - rejects a string, boolean, NaN, and infinite threshold_pct
  - rejects threshold_pct of 0, a negative number, and 100.1; accepts 100
    as the inclusive upper bound
  - accepts each of daily/weekly/monthly as a cadence and rejects an
    unknown cadence and a raw cron string under a "cron" key (D-11)
  - rejects a non-dict config (None, a list, a string) for every rule type
  - asserts the returned dict is a distinct object from the input

Group B — ``latest_memo_status_by_ticker``, DB-backed (``db_session``
fixture, ``@pytest.mark.anyio``). Seeds rows through the real FK chain
(User, Company, ResearchRequest, ResearchPlan, ResearchMemo), mirroring
``tests/api/test_memo_routes.py``'s and ``tests/services/test_company_service.py``'s
seeding style:
  - returns {} for an empty ticker sequence
  - returns the most recent memo's status value and ISO date for a ticker
    with two memos of different created_at (the newer one wins)
  - omits a ticker that has no memo at all (the D-13 "no research yet" case)
  - omits a memo belonging to a DIFFERENT user for the same ticker — the
    T-10-03-MEMOLEAK regression test; asserts the returned dict is empty
  - omits a soft-deleted memo even when it is the newest, falling back to
    the next-newest non-deleted memo
  - resolves several tickers (including one with no memo) in one call
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AlertRuleType,
    Company,
    ResearchMemo,
    ResearchMemoStatus,
    ResearchPlan,
    ResearchRequest,
    User,
)
from app.services.watchlist_service import (
    InvalidRuleConfigError,
    latest_memo_status_by_ticker,
    validate_rule_config,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Group A: validate_rule_config (DB-free)
# ---------------------------------------------------------------------------


def test_new_filing_accepts_empty_config_and_returns_empty_dict() -> None:
    assert validate_rule_config(AlertRuleType.NEW_FILING, {}) == {}


def test_new_filing_rejects_config_carrying_any_key() -> None:
    with pytest.raises(InvalidRuleConfigError):
        validate_rule_config(AlertRuleType.NEW_FILING, {"filing_type": "10-K"})


def test_price_move_accepts_valid_config_and_normalises_int_threshold_to_float() -> None:
    result = validate_rule_config(
        AlertRuleType.PRICE_MOVE, {"threshold_pct": 5, "direction": "down"}
    )
    assert result == {"threshold_pct": 5.0, "direction": "down"}
    assert isinstance(result["threshold_pct"], float)


@pytest.mark.parametrize("direction", ["up", "down", "either"])
def test_price_move_accepts_each_valid_direction(direction: str) -> None:
    result = validate_rule_config(
        AlertRuleType.PRICE_MOVE, {"threshold_pct": 5, "direction": direction}
    )
    assert result["direction"] == direction


def test_price_move_rejects_unknown_direction() -> None:
    with pytest.raises(InvalidRuleConfigError):
        validate_rule_config(
            AlertRuleType.PRICE_MOVE, {"threshold_pct": 5, "direction": "sideways"}
        )


def test_price_move_rejects_missing_direction_key() -> None:
    with pytest.raises(InvalidRuleConfigError):
        validate_rule_config(AlertRuleType.PRICE_MOVE, {"threshold_pct": 5})


def test_price_move_rejects_missing_threshold_pct_key() -> None:
    with pytest.raises(InvalidRuleConfigError):
        validate_rule_config(AlertRuleType.PRICE_MOVE, {"direction": "up"})


def test_price_move_rejects_extra_unrecognised_key() -> None:
    with pytest.raises(InvalidRuleConfigError):
        validate_rule_config(
            AlertRuleType.PRICE_MOVE,
            {"threshold_pct": 5, "direction": "up", "extra": 1},
        )


@pytest.mark.parametrize(
    "bad_threshold",
    ["5", True, float("nan"), float("inf")],
    ids=["string", "boolean", "nan", "infinite"],
)
def test_price_move_rejects_non_numeric_threshold(bad_threshold: object) -> None:
    with pytest.raises(InvalidRuleConfigError):
        validate_rule_config(
            AlertRuleType.PRICE_MOVE,
            {"threshold_pct": bad_threshold, "direction": "up"},
        )


@pytest.mark.parametrize(
    "threshold,should_accept",
    [(0, False), (-3, False), (100.1, False), (100, True)],
    ids=["zero", "negative", "above-max", "inclusive-max"],
)
def test_price_move_threshold_boundary(threshold: float, should_accept: bool) -> None:
    config = {"threshold_pct": threshold, "direction": "up"}
    if should_accept:
        result = validate_rule_config(AlertRuleType.PRICE_MOVE, config)
        assert result["threshold_pct"] == float(threshold)
    else:
        with pytest.raises(InvalidRuleConfigError):
            validate_rule_config(AlertRuleType.PRICE_MOVE, config)


@pytest.mark.parametrize("cadence", ["daily", "weekly", "monthly"])
def test_scheduled_accepts_each_valid_cadence(cadence: str) -> None:
    assert validate_rule_config(AlertRuleType.SCHEDULED, {"cadence": cadence}) == {
        "cadence": cadence
    }


def test_scheduled_rejects_unknown_cadence() -> None:
    with pytest.raises(InvalidRuleConfigError):
        validate_rule_config(AlertRuleType.SCHEDULED, {"cadence": "hourly"})


def test_scheduled_rejects_raw_cron_key() -> None:
    with pytest.raises(InvalidRuleConfigError):
        validate_rule_config(AlertRuleType.SCHEDULED, {"cron": "0 9 * * *"})


@pytest.mark.parametrize(
    "rule_type",
    [AlertRuleType.NEW_FILING, AlertRuleType.PRICE_MOVE, AlertRuleType.SCHEDULED],
)
@pytest.mark.parametrize(
    "bad_config", [None, [], "not-a-dict"], ids=["none", "list", "string"]
)
def test_validate_rule_config_rejects_non_dict_config(
    rule_type: AlertRuleType, bad_config: object
) -> None:
    with pytest.raises(InvalidRuleConfigError):
        validate_rule_config(rule_type, bad_config)


def test_validate_rule_config_returns_distinct_object_from_input() -> None:
    original = {"threshold_pct": 5, "direction": "up"}
    result = validate_rule_config(AlertRuleType.PRICE_MOVE, original)
    original["direction"] = "down"
    assert result["direction"] == "up"


# ---------------------------------------------------------------------------
# Group B: latest_memo_status_by_ticker (DB-backed)
# ---------------------------------------------------------------------------


async def _seed_user(db_session: AsyncSession) -> User:
    """Persist and return a User row (FK target for ResearchMemo's user_id)."""
    user = User(email=f"{uuid.uuid4()}@example.com", password_hash="not-a-real-hash")
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


async def _seed_company(db_session: AsyncSession, ticker: str) -> Company:
    """Persist and return a Company row (FK parent for ResearchMemo.ticker)."""
    company = Company(ticker=ticker, name=f"{ticker} Inc.")
    db_session.add(company)
    await db_session.flush()
    return company


async def _seed_research_plan(
    db_session: AsyncSession, owner: User, ticker: str
) -> ResearchPlan:
    """Persist a ResearchRequest + owning ResearchPlan row (the FK a
    ResearchMemo hangs off), mirroring
    ``tests/api/test_research_api.py::_seed_research_plan``.
    """
    request = ResearchRequest(
        user_id=owner.id, raw_query=f"Tell me about {ticker}", status="RESOLVED"
    )
    db_session.add(request)
    await db_session.flush()

    plan = ResearchPlan(
        request_id=request.id, user_id=owner.id, resolved_tickers=[ticker]
    )
    db_session.add(plan)
    await db_session.flush()
    await db_session.refresh(plan)
    return plan


async def _seed_memo(
    db_session: AsyncSession,
    plan: ResearchPlan,
    owner: User,
    *,
    ticker: str,
    status: ResearchMemoStatus = ResearchMemoStatus.COMPLETE,
    deleted_at: datetime | None = None,
) -> ResearchMemo:
    """Persist a ResearchMemo and commit in its own transaction, so
    ``created_at`` (Postgres ``now()``, fixed per transaction) differs
    across sequential calls — required for the newest-wins assertions
    below (mirrors ``tests/api/test_memo_routes.py::_seed_memo``).
    """
    memo = ResearchMemo(
        plan_id=plan.id,
        user_id=owner.id,
        ticker=ticker,
        status=status,
        body={},
        deleted_at=deleted_at,
    )
    db_session.add(memo)
    await db_session.commit()
    await db_session.refresh(memo)
    return memo


async def test_returns_empty_dict_for_empty_ticker_sequence(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    await db_session.commit()

    result = await latest_memo_status_by_ticker([], user.id, db_session)

    assert result == {}


async def test_returns_newest_memo_status_and_date_for_ticker_with_two_memos(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    await _seed_company(db_session, "AAPL")
    plan = await _seed_research_plan(db_session, user, "AAPL")
    await db_session.commit()

    await _seed_memo(
        db_session, plan, user, ticker="AAPL", status=ResearchMemoStatus.PARTIAL
    )
    newest = await _seed_memo(
        db_session, plan, user, ticker="AAPL", status=ResearchMemoStatus.COMPLETE
    )

    result = await latest_memo_status_by_ticker(["AAPL"], user.id, db_session)

    assert result == {
        "AAPL": (ResearchMemoStatus.COMPLETE.value, newest.created_at.isoformat())
    }


async def test_omits_ticker_with_no_memo(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    await _seed_company(db_session, "AAPL")
    await db_session.commit()

    result = await latest_memo_status_by_ticker(["AAPL"], user.id, db_session)

    assert result == {}


async def test_omits_memo_belonging_to_different_user(db_session: AsyncSession) -> None:
    """T-10-03-MEMOLEAK regression: watchlisting a ticker someone else
    already researched must not surface that other user's memo status/date.
    Fails if the ``user_id`` predicate is removed from the query.
    """
    owner = await _seed_user(db_session)
    other_user = await _seed_user(db_session)
    await _seed_company(db_session, "AAPL")
    other_plan = await _seed_research_plan(db_session, other_user, "AAPL")
    await db_session.commit()
    await _seed_memo(db_session, other_plan, other_user, ticker="AAPL")

    result = await latest_memo_status_by_ticker(["AAPL"], owner.id, db_session)

    assert result == {}


async def test_omits_soft_deleted_memo_even_when_newest(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    await _seed_company(db_session, "AAPL")
    plan = await _seed_research_plan(db_session, user, "AAPL")
    await db_session.commit()

    older = await _seed_memo(
        db_session, plan, user, ticker="AAPL", status=ResearchMemoStatus.PARTIAL
    )
    await _seed_memo(
        db_session,
        plan,
        user,
        ticker="AAPL",
        status=ResearchMemoStatus.COMPLETE,
        deleted_at=datetime.now(UTC),
    )

    result = await latest_memo_status_by_ticker(["AAPL"], user.id, db_session)

    # The newer, soft-deleted row must be excluded entirely — the result
    # falls back to the older, non-deleted row, not to an empty result.
    assert result == {
        "AAPL": (ResearchMemoStatus.PARTIAL.value, older.created_at.isoformat())
    }


async def test_resolves_several_tickers_in_one_call(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    await _seed_company(db_session, "AAPL")
    await _seed_company(db_session, "MSFT")
    plan_aapl = await _seed_research_plan(db_session, user, "AAPL")
    plan_msft = await _seed_research_plan(db_session, user, "MSFT")
    await db_session.commit()

    aapl_memo = await _seed_memo(db_session, plan_aapl, user, ticker="AAPL")
    msft_memo = await _seed_memo(db_session, plan_msft, user, ticker="MSFT")

    result = await latest_memo_status_by_ticker(
        ["AAPL", "MSFT", "GOOG"], user.id, db_session
    )

    assert result == {
        "AAPL": (aapl_memo.status.value, aapl_memo.created_at.isoformat()),
        "MSFT": (msft_memo.status.value, msft_memo.created_at.isoformat()),
    }
