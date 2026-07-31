"""ORM-level model tests for Phase 11's persistence layer (11-01-PLAN.md,
WATCH-06, WATCH-07, WATCH-09).

Coverage:
  - `AlertEvent` persists with a default-false `read` flag and a
    timezone-aware `triggered_at` (D-06).
  - `AlertEvent` rows cascade-delete when their parent `AlertRule` is
    deleted (D-06) — proves the `ondelete="CASCADE"` FK.
  - `AlertEvent` rows cascade-delete when the grandparent `WatchlistEntry`
    is deleted — the two-level cascade the ownership chain depends on.
  - `AlertEvent` rows survive a parent `AlertRule` being soft-disabled
    (D-07 — disabling never removes history).
  - `AlertRule.state` defaults to `NULL` and round-trips a JSON dict
    (D-03).
  - `AlertRule.state` requires a NEW dict assignment to persist a change —
    in-place mutation of a plain `JSON` column is not tracked by
    SQLAlchemy, and this test pins that hazard for every future writer
    (the Phase 11 evaluator).

Seeds the `User -> Company -> WatchlistEntry -> AlertRule` chain directly
through the `db_session` fixture, reusing `_seed_user`/`_seed_company` from
`tests.api.test_research_api` the same way `tests/api/test_watchlist_api.py`
does. Skips automatically when test-postgres (port 5433) is unreachable,
via the `db_session` fixture's built-in skip behavior.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AlertEvent, AlertRule, AlertRuleType, WatchlistEntry
from tests.api.test_research_api import _seed_company, _seed_user


async def _seed_watchlist_entry_and_rule(
    db_session: AsyncSession,
    ticker: str = "AAPL",
    rule_type: AlertRuleType = AlertRuleType.NEW_FILING,
    config: dict | None = None,
) -> tuple[WatchlistEntry, AlertRule]:
    """Seed a User -> Company -> WatchlistEntry -> AlertRule chain and commit it."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker=ticker)
    entry = WatchlistEntry(user_id=user.id, ticker=ticker)
    db_session.add(entry)
    await db_session.flush()
    rule = AlertRule(
        watchlist_id=entry.id,
        rule_type=rule_type,
        config=config if config is not None else {},
    )
    db_session.add(rule)
    await db_session.commit()
    await db_session.refresh(entry)
    await db_session.refresh(rule)
    return entry, rule


# ---------------------------------------------------------------------------
# AlertEvent (D-06)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_alert_event_persists_and_defaults_read_false(
    db_session: AsyncSession,
) -> None:
    """An AlertEvent with only alert_rule_id + message defaults read=False
    and gets a server-generated, timezone-aware triggered_at."""
    _entry, rule = await _seed_watchlist_entry_and_rule(db_session)

    event = AlertEvent(alert_rule_id=rule.id, message="AAPL dropped 6.2%")
    db_session.add(event)
    await db_session.commit()
    await db_session.refresh(event)

    assert event.read is False
    assert event.triggered_at is not None
    assert event.triggered_at.tzinfo is not None


@pytest.mark.anyio
async def test_alert_event_cascades_when_parent_rule_deleted(
    db_session: AsyncSession,
) -> None:
    """Deleting the parent AlertRule removes all its AlertEvent rows (D-06)."""
    _entry, rule = await _seed_watchlist_entry_and_rule(db_session)

    event1 = AlertEvent(alert_rule_id=rule.id, message="first trigger")
    event2 = AlertEvent(alert_rule_id=rule.id, message="second trigger")
    db_session.add_all([event1, event2])
    await db_session.commit()

    await db_session.delete(rule)
    await db_session.commit()

    result = await db_session.execute(
        select(AlertEvent).where(AlertEvent.alert_rule_id == rule.id)
    )
    assert result.scalars().all() == []


@pytest.mark.anyio
async def test_alert_event_cascades_when_watchlist_entry_deleted(
    db_session: AsyncSession,
) -> None:
    """Deleting the grandparent WatchlistEntry removes both the AlertRule
    and its AlertEvent rows (the two-level cascade ownership depends on)."""
    entry, rule = await _seed_watchlist_entry_and_rule(db_session)
    event = AlertEvent(alert_rule_id=rule.id, message="trigger before deletion")
    db_session.add(event)
    await db_session.commit()

    await db_session.delete(entry)
    await db_session.commit()

    rule_result = await db_session.execute(
        select(AlertRule).where(AlertRule.id == rule.id)
    )
    assert rule_result.scalar_one_or_none() is None

    event_result = await db_session.execute(
        select(AlertEvent).where(AlertEvent.alert_rule_id == rule.id)
    )
    assert event_result.scalars().all() == []


@pytest.mark.anyio
async def test_alert_event_survives_parent_rule_disable(
    db_session: AsyncSession,
) -> None:
    """Soft-disabling the parent AlertRule (enabled=False) never removes its
    AlertEvent history (D-07)."""
    _entry, rule = await _seed_watchlist_entry_and_rule(db_session)
    event = AlertEvent(alert_rule_id=rule.id, message="trigger before disable")
    db_session.add(event)
    await db_session.commit()

    rule.enabled = False
    await db_session.commit()

    result = await db_session.execute(
        select(AlertEvent).where(AlertEvent.alert_rule_id == rule.id)
    )
    assert len(result.scalars().all()) == 1


# ---------------------------------------------------------------------------
# AlertRule.state (D-03)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_alert_rule_state_defaults_to_null_and_round_trips_json(
    db_session: AsyncSession,
) -> None:
    """A freshly inserted AlertRule has state=None; assigning a dict with
    last_price/last_seen_accession/last_checked_at round-trips through
    commit + identity-map expiry + re-select (D-03)."""
    _entry, rule = await _seed_watchlist_entry_and_rule(db_session)
    assert rule.state is None
    rule_id = rule.id

    new_state = {
        "last_price": 172.34,
        "last_seen_accession": "0000320193-26-000012",
        "last_checked_at": "2026-07-30T12:00:00+00:00",
    }
    rule.state = new_state
    await db_session.commit()
    db_session.expire_all()

    result = await db_session.execute(select(AlertRule).where(AlertRule.id == rule_id))
    reloaded = result.scalar_one()
    assert reloaded.state == new_state


@pytest.mark.anyio
async def test_alert_rule_state_requires_new_dict_assignment(
    db_session: AsyncSession,
) -> None:
    """Mutating the loaded state dict in place does NOT persist (plain JSON
    columns are not mutation-tracked by SQLAlchemy); assigning a brand-new
    dict DOES persist. This pins the mutation-tracking hazard every future
    evaluator writer must respect."""
    _entry, rule = await _seed_watchlist_entry_and_rule(db_session)
    rule_id = rule.id
    rule.state = {"last_triggered_at": "2026-07-01T00:00:00+00:00"}
    await db_session.commit()
    db_session.expire_all()

    result = await db_session.execute(select(AlertRule).where(AlertRule.id == rule_id))
    reloaded = result.scalar_one()

    # In-place mutation — must NOT persist.
    reloaded.state["last_triggered_at"] = "2026-07-15T00:00:00+00:00"
    await db_session.commit()
    db_session.expire_all()

    result = await db_session.execute(select(AlertRule).where(AlertRule.id == rule_id))
    unchanged = result.scalar_one()
    assert unchanged.state == {"last_triggered_at": "2026-07-01T00:00:00+00:00"}

    # New dict assignment — MUST persist.
    unchanged.state = {"last_triggered_at": "2026-07-15T00:00:00+00:00"}
    await db_session.commit()
    db_session.expire_all()

    result = await db_session.execute(select(AlertRule).where(AlertRule.id == rule_id))
    changed = result.scalar_one()
    assert changed.state == {"last_triggered_at": "2026-07-15T00:00:00+00:00"}
