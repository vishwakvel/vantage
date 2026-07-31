"""Integration tests for app.services.alert_evaluation_service — WATCH-06/WATCH-09.

Coverage (11-06-PLAN.md, Task 1 — dispatcher isolation, NEW_FILING, SCHEDULED):
  TestDispatcher:
    - empty database, disabled-rule external-call avoidance (WATCH-08
      interaction), one-pass-all-three-types (D-04), per-rule isolation on
      EDGAR failure (a poison rule never discards a sibling's committed
      state), publish-failure durability (D-09's durable-row-is-truth), and
      commit-before-publish ordering proven via a second, independent
      session/connection.
  TestNewFiling:
    - seed-without-notifying (D-02), fire-once-and-advance-cursor,
      unchanged-accession no-op, repeated-tick dedup (D-05), newest-hit
      selection by file_date (not response order), zero-hits/EDGAR-failure
      cursor preservation, and accession-less hit skipping.
  TestScheduled:
    - no-immediate-fire on a brand new rule, fire-once-cadence-elapsed,
      no-refire-before-next-cadence (D-05), refire-after-another-interval,
      each fixed cadence preset (daily/weekly/monthly), and D-01's
      never-launches-research non-goal asserted as a database fact.

Mocking policy: the DB session is REAL (test-postgres, port 5433, via the
``db_session`` fixture from ``tests/conftest.py``). Only the three external
collaborators are mocked: ``app.services.alert_evaluation_service.edgar_client``
(patched at ``.get``), ``...live_price_source`` (patched at
``.get_current_price``), and ``...publish_notification``. Every state
assertion goes through ``_reload`` (an ``expire_all`` + re-select) rather than
the in-memory ORM instance — asserting against the in-memory object would
pass even when a JSON column write was silently dropped at commit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import (
    AlertEvent,
    AlertRule,
    AlertRuleType,
    Company,
    ResearchMemo,
    ResearchPlan,
    ResearchRequest,
    WatchlistEntry,
)
from app.services.alert_evaluation_service import evaluate_all_rules
from tests.api.test_research_api import _seed_company, _seed_user
from tests.conftest import TEST_DATABASE_URL

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_rule(
    session: AsyncSession,
    rule_type: AlertRuleType,
    config: dict,
    *,
    user=None,
    ticker: str = "AAPL",
    created_at: datetime | None = None,
    state: dict | None = None,
) -> AlertRule:
    """Seed the full User -> Company -> WatchlistEntry -> AlertRule chain.

    Accepts an explicit ``created_at`` so SCHEDULED due-ness can be tested by
    backdating rather than by sleeping. Bypasses
    ``watchlist_service.validate_rule_config`` entirely — this is a raw ORM
    insert, so a test can deliberately seed a data-drift config (e.g. an
    unrecognised PRICE_MOVE direction) that the creation route would reject.
    """
    if user is None:
        user = await _seed_user(session)

    existing = await session.execute(select(Company).where(Company.ticker == ticker))
    if existing.scalar_one_or_none() is None:
        await _seed_company(session, ticker=ticker, name=f"{ticker} Inc.")

    entry = WatchlistEntry(user_id=user.id, ticker=ticker)
    session.add(entry)
    await session.flush()

    rule = AlertRule(
        watchlist_id=entry.id,
        rule_type=rule_type,
        config=config,
        state=state,
        enabled=True,
    )
    if created_at is not None:
        rule.created_at = created_at
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return rule


def _efts_response(
    *, adsh: str, form: str = "10-Q", file_date: str = "2026-07-28", extra_hits=()
) -> MagicMock:
    """Return a fake EFTS hits payload matching ingestion_service.py's field names."""
    hit = {
        "_source": {
            "adsh": adsh,
            "form": form,
            "file_date": file_date,
            "root_forms": [form],
            "period_ending": file_date,
        }
    }
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"hits": {"hits": [hit, *extra_hits]}})
    return resp


async def _reload(session: AsyncSession, model, pk):
    """Expire the identity map and re-select *pk* — a real DB round trip.

    A JSON column write that failed to mark the attribute dirty still looks
    correct against the in-memory instance and only reveals itself after a
    round trip like this one.
    """
    session.expire_all()
    return await session.get(model, pk)


async def _count_events(session: AsyncSession, rule_id) -> int:
    result = await session.execute(
        select(func.count()).select_from(AlertEvent).where(AlertEvent.alert_rule_id == rule_id)
    )
    return result.scalar_one()


@pytest.fixture()
def mocks():
    """Patch the three external collaborators; the session stays real."""
    with (
        patch(
            "app.services.alert_evaluation_service.edgar_client.get",
            new_callable=AsyncMock,
        ) as edgar_get,
        patch(
            "app.services.alert_evaluation_service.live_price_source.get_current_price",
            new_callable=AsyncMock,
        ) as get_price,
        patch(
            "app.services.alert_evaluation_service.publish_notification",
            new_callable=AsyncMock,
        ) as publish,
    ):
        yield type("Mocks", (), {"edgar_get": edgar_get, "get_price": get_price, "publish": publish})()


# ---------------------------------------------------------------------------
# TestDispatcher
# ---------------------------------------------------------------------------


class TestDispatcher:
    @pytest.mark.anyio
    async def test_empty_database_returns_zero_and_publishes_nothing(self, db_session, mocks):
        fired = await evaluate_all_rules(db_session)

        assert fired == 0
        mocks.publish.assert_not_awaited()

    @pytest.mark.anyio
    async def test_disabled_rule_is_never_evaluated(self, db_session, mocks):
        """WATCH-08 interaction: a disabled rule must consume no external call."""
        rule = await _seed_rule(
            db_session, AlertRuleType.PRICE_MOVE, {"threshold_pct": 5.0, "direction": "down"}
        )
        rule.enabled = False
        await db_session.commit()

        fired = await evaluate_all_rules(db_session)

        assert fired == 0
        mocks.get_price.assert_not_awaited()
        assert await _count_events(db_session, rule.id) == 0
        reloaded = await _reload(db_session, AlertRule, rule.id)
        assert reloaded.state is None

    @pytest.mark.anyio
    async def test_only_enabled_rules_of_all_three_types_are_evaluated_in_one_pass(
        self, db_session, mocks
    ):
        """D-04's single-pass, all-types contract."""
        mocks.edgar_get.return_value = _efts_response(adsh="0001-A")
        mocks.get_price.return_value = 100.0

        user = await _seed_user(db_session)
        await db_session.commit()

        nf_rule = await _seed_rule(
            db_session, AlertRuleType.NEW_FILING, {}, user=user, ticker="AAPL"
        )
        pm_rule = await _seed_rule(
            db_session,
            AlertRuleType.PRICE_MOVE,
            {"threshold_pct": 5.0, "direction": "down"},
            user=user,
            ticker="MSFT",
        )
        sched_rule = await _seed_rule(
            db_session,
            AlertRuleType.SCHEDULED,
            {"cadence": "daily"},
            user=user,
            ticker="TSLA",
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        disabled_rule = await _seed_rule(
            db_session,
            AlertRuleType.PRICE_MOVE,
            {"threshold_pct": 5.0, "direction": "down"},
            user=user,
            ticker="NVDA",
        )
        disabled_rule.enabled = False
        await db_session.commit()

        # Capture ids before any reload — `_reload`'s `expire_all()` expires
        # every object tracked by the session, not just the one being
        # reloaded, so dereferencing a sibling ORM object's `.id` afterward
        # would itself trigger an (unsupported, synchronous) lazy reload.
        nf_id, pm_id, sched_id, disabled_id = (
            nf_rule.id,
            pm_rule.id,
            sched_rule.id,
            disabled_rule.id,
        )

        fired = await evaluate_all_rules(db_session)

        assert fired == 1  # only the SCHEDULED rule notifies this tick
        mocks.edgar_get.assert_awaited_once()
        mocks.get_price.assert_awaited_once()

        # Each `_reload` call expires every object the session tracks (not
        # just the one being fetched), so a subsequent `_reload` for a
        # different rule would itself invalidate an earlier reloaded
        # instance still in scope — read the field out to a plain value
        # immediately after each individual reload rather than holding
        # multiple reloaded ORM objects at once.
        nf_state = (await _reload(db_session, AlertRule, nf_id)).state
        pm_state = (await _reload(db_session, AlertRule, pm_id)).state
        sched_state = (await _reload(db_session, AlertRule, sched_id)).state
        disabled_state = (await _reload(db_session, AlertRule, disabled_id)).state

        assert nf_state is not None
        assert pm_state is not None
        assert sched_state is not None
        assert disabled_state is None

    @pytest.mark.anyio
    async def test_failing_rule_does_not_abort_tick_or_discard_sibling_state(
        self, db_session, mocks
    ):
        now = datetime.now(timezone.utc)
        rule_a = await _seed_rule(
            db_session,
            AlertRuleType.NEW_FILING,
            {},
            ticker="AAPL",
            created_at=now - timedelta(minutes=2),
        )
        rule_b = await _seed_rule(
            db_session,
            AlertRuleType.NEW_FILING,
            {},
            ticker="MSFT",
            created_at=now - timedelta(minutes=1),
        )

        mocks.edgar_get.side_effect = [RuntimeError("EDGAR down"), _efts_response(adsh="0001-B")]
        rule_a_id, rule_b_id = rule_a.id, rule_b.id

        fired = await evaluate_all_rules(db_session)

        assert fired == 0
        a_state = (await _reload(db_session, AlertRule, rule_a_id)).state
        b_state = (await _reload(db_session, AlertRule, rule_b_id)).state
        assert a_state is None
        assert b_state is not None
        assert b_state["last_seen_accession"] == "0001-B"

    @pytest.mark.anyio
    async def test_publish_failure_does_not_lose_the_event(self, db_session, mocks):
        """D-09: the durable row is the source of truth, the push is best-effort."""
        mocks.publish.side_effect = RuntimeError("redis down")
        rule = await _seed_rule(
            db_session,
            AlertRuleType.SCHEDULED,
            {"cadence": "daily"},
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )

        fired = await evaluate_all_rules(db_session)

        assert fired == 1
        assert await _count_events(db_session, rule.id) == 1

    @pytest.mark.anyio
    async def test_event_row_is_committed_before_publish(self, db_session, mocks):
        """Proves commit-before-publish with a SECOND, independent session."""
        rule = await _seed_rule(
            db_session,
            AlertRuleType.SCHEDULED,
            {"cadence": "daily"},
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )

        observed = {}

        async def _check_committed_from_second_session(*args, **kwargs):
            engine = create_async_engine(TEST_DATABASE_URL, echo=False)
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as verify_session:
                observed["count"] = await _count_events(verify_session, rule.id)
            await engine.dispose()

        mocks.publish.side_effect = _check_committed_from_second_session

        await evaluate_all_rules(db_session)

        assert observed["count"] == 1


# ---------------------------------------------------------------------------
# TestNewFiling
# ---------------------------------------------------------------------------


class TestNewFiling:
    @pytest.mark.anyio
    async def test_first_evaluation_seeds_cursor_without_notifying(self, db_session, mocks):
        mocks.edgar_get.return_value = _efts_response(adsh="0001-NEW")
        rule = await _seed_rule(db_session, AlertRuleType.NEW_FILING, {})

        fired = await evaluate_all_rules(db_session)

        assert fired == 0
        assert await _count_events(db_session, rule.id) == 0
        mocks.publish.assert_not_awaited()
        reloaded = await _reload(db_session, AlertRule, rule.id)
        assert reloaded.state["last_seen_accession"] == "0001-NEW"

    @pytest.mark.anyio
    async def test_new_accession_fires_once_and_advances_cursor(self, db_session, mocks):
        mocks.edgar_get.return_value = _efts_response(adsh="0002-NEW", form="10-Q")
        rule = await _seed_rule(
            db_session, AlertRuleType.NEW_FILING, {}, state={"last_seen_accession": "0000-OLD"}
        )

        fired = await evaluate_all_rules(db_session)

        assert fired == 1
        assert await _count_events(db_session, rule.id) == 1
        mocks.publish.assert_awaited_once()
        result = await db_session.execute(
            select(AlertEvent).where(AlertEvent.alert_rule_id == rule.id)
        )
        event = result.scalar_one()
        assert "10-Q" in event.message
        assert "AAPL" in event.message
        reloaded = await _reload(db_session, AlertRule, rule.id)
        assert reloaded.state["last_seen_accession"] == "0002-NEW"

    @pytest.mark.anyio
    async def test_unchanged_accession_does_not_fire(self, db_session, mocks):
        mocks.edgar_get.return_value = _efts_response(adsh="0001-SAME")
        rule = await _seed_rule(
            db_session, AlertRuleType.NEW_FILING, {}, state={"last_seen_accession": "0001-SAME"}
        )

        fired = await evaluate_all_rules(db_session)

        assert fired == 0
        assert await _count_events(db_session, rule.id) == 0
        reloaded = await _reload(db_session, AlertRule, rule.id)
        assert reloaded.state["last_checked_at"] is not None

    @pytest.mark.anyio
    async def test_repeated_ticks_with_same_filing_fire_only_once(self, db_session, mocks):
        """D-05 self-re-arming for the filing cursor."""
        mocks.edgar_get.return_value = _efts_response(adsh="0003-NEW")
        rule = await _seed_rule(
            db_session, AlertRuleType.NEW_FILING, {}, state={"last_seen_accession": "0000-OLD"}
        )

        await evaluate_all_rules(db_session)
        await evaluate_all_rules(db_session)
        await evaluate_all_rules(db_session)

        assert await _count_events(db_session, rule.id) == 1

    @pytest.mark.anyio
    async def test_newest_hit_selected_by_file_date_not_response_order(self, db_session, mocks):
        """EFTS orders by relevance, not date."""
        newer_hit = {
            "_source": {
                "adsh": "0005-NEWER",
                "form": "10-Q",
                "file_date": "2026-07-01",
                "root_forms": ["10-Q"],
                "period_ending": "2026-06-30",
            }
        }
        # The FIRST element (this primary hit) is deliberately the OLDER one.
        mocks.edgar_get.return_value = _efts_response(
            adsh="0004-OLDER", form="10-K", file_date="2026-01-01", extra_hits=(newer_hit,)
        )
        rule = await _seed_rule(db_session, AlertRuleType.NEW_FILING, {})

        await evaluate_all_rules(db_session)

        reloaded = await _reload(db_session, AlertRule, rule.id)
        assert reloaded.state["last_seen_accession"] == "0005-NEWER"

    @pytest.mark.anyio
    async def test_zero_hits_leaves_cursor_untouched(self, db_session, mocks):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"hits": {"hits": []}})
        mocks.edgar_get.return_value = resp
        rule = await _seed_rule(
            db_session, AlertRuleType.NEW_FILING, {}, state={"last_seen_accession": "0001-EXIST"}
        )

        await evaluate_all_rules(db_session)

        reloaded = await _reload(db_session, AlertRule, rule.id)
        assert reloaded.state["last_seen_accession"] == "0001-EXIST"
        assert reloaded.state["last_checked_at"] is not None

    @pytest.mark.anyio
    async def test_edgar_failure_leaves_cursor_untouched(self, db_session, mocks):
        mocks.edgar_get.side_effect = RuntimeError("EDGAR down")
        rule = await _seed_rule(
            db_session, AlertRuleType.NEW_FILING, {}, state={"last_seen_accession": "0001-EXIST"}
        )
        # Captured before the call — the dispatcher's per-rule rollback on
        # the EDGAR failure expires this instance's attributes.
        rule_id = rule.id

        fired = await evaluate_all_rules(db_session)

        assert fired == 0
        mocks.publish.assert_not_awaited()
        reloaded = await _reload(db_session, AlertRule, rule_id)
        assert reloaded.state["last_seen_accession"] == "0001-EXIST"

    @pytest.mark.anyio
    async def test_hit_without_accession_is_skipped(self, db_session, mocks):
        mocks.edgar_get.return_value = _efts_response(adsh="")
        rule = await _seed_rule(db_session, AlertRuleType.NEW_FILING, {})

        fired = await evaluate_all_rules(db_session)

        assert fired == 0
        assert await _count_events(db_session, rule.id) == 0


# ---------------------------------------------------------------------------
# TestScheduled
# ---------------------------------------------------------------------------


class TestScheduled:
    @pytest.mark.anyio
    async def test_new_rule_does_not_fire_immediately(self, db_session, mocks):
        rule = await _seed_rule(
            db_session,
            AlertRuleType.SCHEDULED,
            {"cadence": "daily"},
            created_at=datetime.now(timezone.utc),
        )

        fired = await evaluate_all_rules(db_session)

        assert fired == 0
        assert await _count_events(db_session, rule.id) == 0

    @pytest.mark.anyio
    async def test_fires_once_cadence_elapsed_since_creation(self, db_session, mocks):
        rule = await _seed_rule(
            db_session,
            AlertRuleType.SCHEDULED,
            {"cadence": "daily"},
            ticker="TSLA",
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )

        fired = await evaluate_all_rules(db_session)

        assert fired == 1
        result = await db_session.execute(
            select(AlertEvent).where(AlertEvent.alert_rule_id == rule.id)
        )
        event = result.scalar_one()
        assert event.message == "Daily check-in due for TSLA"
        reloaded = await _reload(db_session, AlertRule, rule.id)
        assert reloaded.state["last_triggered_at"] is not None

    @pytest.mark.anyio
    async def test_does_not_refire_before_next_cadence(self, db_session, mocks):
        rule = await _seed_rule(
            db_session,
            AlertRuleType.SCHEDULED,
            {"cadence": "daily"},
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )

        await evaluate_all_rules(db_session)
        await evaluate_all_rules(db_session)

        assert await _count_events(db_session, rule.id) == 1

    @pytest.mark.anyio
    async def test_refires_after_another_full_interval(self, db_session, mocks):
        rule = await _seed_rule(
            db_session,
            AlertRuleType.SCHEDULED,
            {"cadence": "weekly"},
            state={
                "last_triggered_at": (
                    datetime.now(timezone.utc) - timedelta(days=8)
                ).isoformat()
            },
        )

        fired = await evaluate_all_rules(db_session)

        assert fired == 1
        assert await _count_events(db_session, rule.id) == 1

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "cadence,interval_days", [("daily", 1), ("weekly", 7), ("monthly", 30)]
    )
    async def test_each_cadence_interval(self, db_session, mocks, cadence, interval_days):
        no_fire_rule = await _seed_rule(
            db_session,
            AlertRuleType.SCHEDULED,
            {"cadence": cadence},
            ticker="AAPL",
            created_at=datetime.now(timezone.utc) - timedelta(days=interval_days - 1),
        )
        fire_rule = await _seed_rule(
            db_session,
            AlertRuleType.SCHEDULED,
            {"cadence": cadence},
            ticker="MSFT",
            created_at=datetime.now(timezone.utc) - timedelta(days=interval_days + 1),
        )

        await evaluate_all_rules(db_session)

        assert await _count_events(db_session, no_fire_rule.id) == 0
        assert await _count_events(db_session, fire_rule.id) == 1

    @pytest.mark.anyio
    async def test_scheduled_fire_launches_no_research(self, db_session, mocks):
        """D-01, asserted as a database fact rather than an import check."""
        rule = await _seed_rule(
            db_session,
            AlertRuleType.SCHEDULED,
            {"cadence": "daily"},
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )

        fired = await evaluate_all_rules(db_session)
        assert fired == 1

        for model in (ResearchRequest, ResearchPlan, ResearchMemo):
            result = await db_session.execute(select(func.count()).select_from(model))
            assert result.scalar_one() == 0
