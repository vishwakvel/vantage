"""Notification and per-ticker alert-history API endpoint tests (11-07-PLAN.md,
WATCH-06, WATCH-07).

Coverage (Task 1 — the three routes built in plan 11-04):
  GET /api/v1/notifications:
    - empty state (200, notifications == [], unread_count == 0 — never 404)
    - only the caller's own events (T-11-04-IDOR three-level ownership join
      proof — the single highest-value test in this class)
    - newest-first ordering
    - the 50-row cap
    - the uncapped unread count (bell badge must not under-report once a
      user crosses the 50-row window)
    - the exact response field-set contract
    - events whose parent rule has since been disabled still appear (D-07)
    - unauthenticated request
  POST /api/v1/notifications/mark-read:
    - marks every unread row and returns the count
    - idempotent second call
    - never touches another user's rows (T-11-04-BULK — the single
      highest-value test in this module; an unrestricted bulk UPDATE would
      pass every other test here)
    - already-read rows are excluded from the returned count
    - unauthenticated request
  GET /api/v1/notifications/history/{entry_id}:
    - owned entry returns 200 with its ticker and events
    - newest-first ordering and the 50-row cap (D-08)
    - events from disabled rules are included (D-07)
    - another entry of the SAME user is excluded
    - another user's entry_id returns 404 (never 403 — T-11-04-IDOR,
      404-never-403 discipline, same detail string as an unknown id)
    - unknown random UUID returns 404
    - a non-UUID path segment returns 422 before any query
    - an owned-but-empty entry returns 200 with an empty list, never 404
    - unauthenticated request

Closes with a round-trip test: seed one event, see it unread via
GET /notifications, mark it read, confirm unread_count drops to 0, and
confirm GET /notifications/history/{entry_id} still returns the same event
— the exact sequence the frontend performs.

Every test drives the composed app via ``create_app()`` (through
``_make_authed_client``/``_make_unauthed_client``), so an unregistered
notifications router would fail every assertion in this module with a 404.

Reuses ``_make_authed_client``, ``_make_unauthed_client``, ``_seed_user``,
and ``_seed_company`` from ``tests.api.test_research_api`` (the same reuse
pattern ``test_watchlist_api.py`` established). Skips automatically when
test-postgres (port 5433) is unreachable, via the ``db_session`` fixture's
built-in skip behavior.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import AlertEvent, AlertRule, AlertRuleType, User, WatchlistEntry
from tests.api.test_research_api import (
    _make_authed_client,
    _make_unauthed_client,
    _seed_company,
    _seed_user,
)

NOTIFICATIONS_URL = "/api/v1/notifications"
MARK_READ_URL = "/api/v1/notifications/mark-read"


def _history_url(entry_id: str) -> str:
    return f"/api/v1/notifications/history/{entry_id}"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_entry(
    db_session: AsyncSession, user: User, ticker: str = "AAPL"
) -> WatchlistEntry:
    """Persist a Company + WatchlistEntry for *user* watching *ticker*."""
    await _seed_company(db_session, ticker=ticker)
    entry = WatchlistEntry(user_id=user.id, ticker=ticker)
    db_session.add(entry)
    await db_session.flush()
    await db_session.refresh(entry)
    return entry


async def _seed_rule(
    db_session: AsyncSession,
    entry: WatchlistEntry,
    rule_type: AlertRuleType = AlertRuleType.NEW_FILING,
    config: dict | None = None,
    enabled: bool = True,
) -> AlertRule:
    """Persist an AlertRule on *entry* — the alert_rule_id every AlertEvent needs."""
    rule = AlertRule(
        watchlist_id=entry.id,
        rule_type=rule_type,
        config=config if config is not None else {},
        enabled=enabled,
    )
    db_session.add(rule)
    await db_session.flush()
    await db_session.refresh(rule)
    return rule


async def _seed_event(
    db_session: AsyncSession,
    rule_id: uuid.UUID,
    message: str,
    triggered_at: datetime,
    read: bool = False,
) -> AlertEvent:
    """Persist one AlertEvent with an explicit ``triggered_at`` so ordering
    assertions are deterministic rather than dependent on insert timing."""
    event = AlertEvent(
        alert_rule_id=rule_id,
        message=message,
        triggered_at=triggered_at,
        read=read,
    )
    db_session.add(event)
    await db_session.flush()
    await db_session.refresh(event)
    return event


_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# GET /api/v1/notifications (WATCH-06)
# ---------------------------------------------------------------------------


class TestListNotifications:
    @pytest.mark.anyio
    async def test_empty_returns_200_with_empty_list_and_zero_unread(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """A user with no events gets 200, an empty list, and zero unread —
        never 404 (mirrors GET /watchlist's empty-state contract)."""
        user = await _seed_user(db_session)
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(NOTIFICATIONS_URL)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["notifications"] == []
        assert body["unread_count"] == 0

    @pytest.mark.anyio
    async def test_returns_only_callers_own_events(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """A second user's events never appear in the caller's response —
        the T-11-04-IDOR proof: the three-level join is the only thing
        standing between the two users' rows."""
        user = await _seed_user(db_session)
        other_user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user, ticker="AAPL")
        other_entry = await _seed_entry(db_session, other_user, ticker="TSLA")
        rule = await _seed_rule(db_session, entry)
        other_rule = await _seed_rule(db_session, other_entry)
        await db_session.commit()

        own_event = await _seed_event(db_session, rule.id, "own", _BASE_TIME)
        await _seed_event(db_session, other_rule.id, "other", _BASE_TIME)
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(NOTIFICATIONS_URL)

        assert resp.status_code == 200, resp.text
        ids = {n["id"] for n in resp.json()["notifications"]}
        assert ids == {str(own_event.id)}

    @pytest.mark.anyio
    async def test_ordered_newest_first(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """Three events with distinct ``triggered_at`` values, inserted out
        of order, are returned in strictly descending order."""
        user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user)
        rule = await _seed_rule(db_session, entry)
        await db_session.commit()

        middle = await _seed_event(
            db_session, rule.id, "middle", _BASE_TIME + timedelta(minutes=5)
        )
        oldest = await _seed_event(db_session, rule.id, "oldest", _BASE_TIME)
        newest = await _seed_event(
            db_session, rule.id, "newest", _BASE_TIME + timedelta(minutes=10)
        )
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(NOTIFICATIONS_URL)

        assert resp.status_code == 200, resp.text
        ids = [n["id"] for n in resp.json()["notifications"]]
        assert ids == [str(newest.id), str(middle.id), str(oldest.id)]

    @pytest.mark.anyio
    async def test_capped_at_50(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """55 events seeded; exactly 50 are returned and the oldest 5 are absent."""
        user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user)
        rule = await _seed_rule(db_session, entry)
        await db_session.commit()

        events = [
            await _seed_event(
                db_session, rule.id, f"event-{i}", _BASE_TIME + timedelta(seconds=i)
            )
            for i in range(55)
        ]
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(NOTIFICATIONS_URL)

        assert resp.status_code == 200, resp.text
        notifications = resp.json()["notifications"]
        assert len(notifications) == 50
        returned_ids = {n["id"] for n in notifications}
        oldest_5_ids = {str(e.id) for e in events[:5]}
        assert oldest_5_ids.isdisjoint(returned_ids)

    @pytest.mark.anyio
    async def test_unread_count_is_not_capped_by_the_50_row_window(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """55 unread events: notifications is capped at 50 but unread_count
        reports the true total of 55 — without this the bell badge would
        under-report once a user crosses the window."""
        user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user)
        rule = await _seed_rule(db_session, entry)
        await db_session.commit()

        for i in range(55):
            await _seed_event(
                db_session,
                rule.id,
                f"event-{i}",
                _BASE_TIME + timedelta(seconds=i),
                read=False,
            )
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(NOTIFICATIONS_URL)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["notifications"]) == 50
        assert body["unread_count"] == 55

    @pytest.mark.anyio
    async def test_response_shape_matches_contract(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """Every returned item's key set is exactly the five-field contract."""
        user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user)
        rule = await _seed_rule(db_session, entry)
        await db_session.commit()

        await _seed_event(db_session, rule.id, "hello", _BASE_TIME)
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(NOTIFICATIONS_URL)

        assert resp.status_code == 200, resp.text
        notifications = resp.json()["notifications"]
        assert len(notifications) == 1
        assert set(notifications[0].keys()) == {
            "id",
            "alert_rule_id",
            "message",
            "triggered_at",
            "read",
        }

    @pytest.mark.anyio
    async def test_includes_events_whose_parent_rule_is_disabled(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """An event whose parent rule is disabled AFTER the trigger still
        appears in the list (D-07)."""
        user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user)
        rule = await _seed_rule(db_session, entry)
        await db_session.commit()

        event = await _seed_event(db_session, rule.id, "still here", _BASE_TIME)
        await db_session.commit()

        await db_session.execute(
            update(AlertRule).where(AlertRule.id == rule.id).values(enabled=False)
        )
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(NOTIFICATIONS_URL)

        assert resp.status_code == 200, resp.text
        ids = {n["id"] for n in resp.json()["notifications"]}
        assert str(event.id) in ids

    @pytest.mark.anyio
    async def test_requires_auth(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """An unauthenticated GET /notifications returns 401/403."""
        async with _make_unauthed_client(db_session, test_settings) as client:
            resp = await client.get(NOTIFICATIONS_URL)

        assert resp.status_code in (401, 403), resp.text


# ---------------------------------------------------------------------------
# POST /api/v1/notifications/mark-read (D-10)
# ---------------------------------------------------------------------------


class TestMarkRead:
    @pytest.mark.anyio
    async def test_marks_all_unread_and_returns_count(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """3 unread + 1 already-read: marked == 3, and a subsequent GET
        reports unread_count == 0."""
        user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user)
        rule = await _seed_rule(db_session, entry)
        await db_session.commit()

        for i in range(3):
            await _seed_event(
                db_session,
                rule.id,
                f"unread-{i}",
                _BASE_TIME + timedelta(seconds=i),
                read=False,
            )
        await _seed_event(
            db_session, rule.id, "already-read", _BASE_TIME + timedelta(seconds=10), read=True
        )
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            mark_resp = await client.post(MARK_READ_URL)
            assert mark_resp.status_code == 200, mark_resp.text
            assert mark_resp.json() == {"marked": 3}

            list_resp = await client.get(NOTIFICATIONS_URL)

        assert list_resp.json()["unread_count"] == 0

    @pytest.mark.anyio
    async def test_second_call_is_idempotent_and_returns_zero(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """A second mark-read call, with nothing left unread, returns marked == 0."""
        user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user)
        rule = await _seed_rule(db_session, entry)
        await db_session.commit()

        await _seed_event(db_session, rule.id, "only", _BASE_TIME, read=False)
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            first = await client.post(MARK_READ_URL)
            second = await client.post(MARK_READ_URL)

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.json() == {"marked": 0}

    @pytest.mark.anyio
    async def test_does_not_touch_other_users_events(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """After the caller's mark-read, a SECOND user's unread events are
        re-selected directly from the session and confirmed untouched
        (T-11-04-BULK) — the single highest-value test in this module: an
        unrestricted bulk UPDATE would pass every other test here."""
        user = await _seed_user(db_session)
        other_user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user, ticker="AAPL")
        other_entry = await _seed_entry(db_session, other_user, ticker="TSLA")
        rule = await _seed_rule(db_session, entry)
        other_rule = await _seed_rule(db_session, other_entry)
        await db_session.commit()

        await _seed_event(db_session, rule.id, "mine", _BASE_TIME, read=False)
        other_event_1 = await _seed_event(
            db_session, other_rule.id, "not-mine-1", _BASE_TIME, read=False
        )
        other_event_2 = await _seed_event(
            db_session, other_rule.id, "not-mine-2", _BASE_TIME + timedelta(seconds=1), read=False
        )
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.post(MARK_READ_URL)

        assert resp.status_code == 200, resp.text

        result = await db_session.execute(
            select(AlertEvent).where(
                AlertEvent.id.in_([other_event_1.id, other_event_2.id])
            )
        )
        other_users_rows = result.scalars().all()
        assert len(other_users_rows) == 2
        assert all(row.read is False for row in other_users_rows)

    @pytest.mark.anyio
    async def test_already_read_rows_are_not_recounted(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """The returned marked count excludes rows that were already read."""
        user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user)
        rule = await _seed_rule(db_session, entry)
        await db_session.commit()

        await _seed_event(db_session, rule.id, "unread", _BASE_TIME, read=False)
        await _seed_event(
            db_session, rule.id, "already-read", _BASE_TIME + timedelta(seconds=1), read=True
        )
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.post(MARK_READ_URL)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"marked": 1}

    @pytest.mark.anyio
    async def test_requires_auth(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """An unauthenticated POST /notifications/mark-read returns 401/403."""
        async with _make_unauthed_client(db_session, test_settings) as client:
            resp = await client.post(MARK_READ_URL)

        assert resp.status_code in (401, 403), resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/notifications/history/{entry_id} (WATCH-07, D-08)
# ---------------------------------------------------------------------------


class TestAlertHistory:
    @pytest.mark.anyio
    async def test_returns_events_for_owned_entry_with_ticker(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """An owned entry returns 200 with its ticker and matching event ids."""
        user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user, ticker="MSFT")
        rule = await _seed_rule(db_session, entry)
        await db_session.commit()

        event = await _seed_event(db_session, rule.id, "hi", _BASE_TIME)
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(_history_url(str(entry.id)))

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ticker"] == "MSFT"
        assert {e["id"] for e in body["events"]} == {str(event.id)}

    @pytest.mark.anyio
    async def test_ordered_newest_first_and_capped_at_50(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """55 events across two rules on the same entry: 50 returned,
        strictly descending (D-08)."""
        user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user)
        rule_a = await _seed_rule(db_session, entry, rule_type=AlertRuleType.NEW_FILING)
        rule_b = await _seed_rule(
            db_session,
            entry,
            rule_type=AlertRuleType.PRICE_MOVE,
            config={"threshold_pct": 5.0, "direction": "up"},
        )
        await db_session.commit()

        events = []
        for i in range(55):
            rule = rule_a if i % 2 == 0 else rule_b
            events.append(
                await _seed_event(
                    db_session, rule.id, f"event-{i}", _BASE_TIME + timedelta(seconds=i)
                )
            )
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(_history_url(str(entry.id)))

        assert resp.status_code == 200, resp.text
        returned = resp.json()["events"]
        assert len(returned) == 50
        returned_times = [e["triggered_at"] for e in returned]
        assert returned_times == sorted(returned_times, reverse=True)
        oldest_5_ids = {str(e.id) for e in events[:5]}
        assert oldest_5_ids.isdisjoint({e["id"] for e in returned})

    @pytest.mark.anyio
    async def test_includes_events_from_disabled_rules(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """Two rules on one entry, one disabled: events from BOTH appear (D-07)."""
        user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user)
        enabled_rule = await _seed_rule(db_session, entry, enabled=True)
        disabled_rule = await _seed_rule(db_session, entry, enabled=False)
        await db_session.commit()

        enabled_event = await _seed_event(
            db_session, enabled_rule.id, "from-enabled", _BASE_TIME
        )
        disabled_event = await _seed_event(
            db_session, disabled_rule.id, "from-disabled", _BASE_TIME + timedelta(seconds=1)
        )
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(_history_url(str(entry.id)))

        assert resp.status_code == 200, resp.text
        ids = {e["id"] for e in resp.json()["events"]}
        assert ids == {str(enabled_event.id), str(disabled_event.id)}

    @pytest.mark.anyio
    async def test_excludes_events_from_another_entry_of_the_same_user(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """Two watchlist entries for one user: the response for one entry
        contains only that entry's events."""
        user = await _seed_user(db_session)
        entry_a = await _seed_entry(db_session, user, ticker="AAPL")
        entry_b = await _seed_entry(db_session, user, ticker="GOOG")
        rule_a = await _seed_rule(db_session, entry_a)
        rule_b = await _seed_rule(db_session, entry_b)
        await db_session.commit()

        event_a = await _seed_event(db_session, rule_a.id, "a-event", _BASE_TIME)
        await _seed_event(db_session, rule_b.id, "b-event", _BASE_TIME)
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(_history_url(str(entry_a.id)))

        assert resp.status_code == 200, resp.text
        ids = {e["id"] for e in resp.json()["events"]}
        assert ids == {str(event_a.id)}

    @pytest.mark.anyio
    async def test_other_users_entry_returns_404(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """Another user's entry_id returns 404 with the SAME detail string
        as an unknown id, and their events are never returned in any form
        (T-11-04-IDOR, 404-never-403)."""
        owner = await _seed_user(db_session)
        other_user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, owner)
        rule = await _seed_rule(db_session, entry)
        await db_session.commit()

        await _seed_event(db_session, rule.id, "owners-event", _BASE_TIME)
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, other_user) as client:
            not_owned_resp = await client.get(_history_url(str(entry.id)))
            unknown_resp = await client.get(_history_url(str(uuid.uuid4())))

        assert not_owned_resp.status_code == 404, not_owned_resp.text
        assert unknown_resp.status_code == 404, unknown_resp.text
        assert not_owned_resp.json()["detail"] == unknown_resp.json()["detail"]
        assert "owners-event" not in not_owned_resp.text

    @pytest.mark.anyio
    async def test_unknown_uuid_returns_404(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """An unknown random UUID returns 404."""
        user = await _seed_user(db_session)
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(_history_url(str(uuid.uuid4())))

        assert resp.status_code == 404, resp.text

    @pytest.mark.anyio
    async def test_non_uuid_segment_returns_422(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """A non-UUID path segment returns 422, not 500."""
        user = await _seed_user(db_session)
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(_history_url("not-a-uuid"))

        assert resp.status_code == 422, resp.text

    @pytest.mark.anyio
    async def test_entry_with_no_events_returns_200_with_empty_list(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """An owned-but-empty entry returns 200 with an empty list, never 404."""
        user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user)
        await db_session.commit()

        async with _make_authed_client(db_session, test_settings, user) as client:
            resp = await client.get(_history_url(str(entry.id)))

        assert resp.status_code == 200, resp.text
        assert resp.json()["events"] == []

    @pytest.mark.anyio
    async def test_requires_auth(
        self, db_session: AsyncSession, test_settings: Settings
    ) -> None:
        """An unauthenticated GET /notifications/history/{entry_id} returns 401/403."""
        user = await _seed_user(db_session)
        entry = await _seed_entry(db_session, user)
        await db_session.commit()

        async with _make_unauthed_client(db_session, test_settings) as client:
            resp = await client.get(_history_url(str(entry.id)))

        assert resp.status_code in (401, 403), resp.text


# ---------------------------------------------------------------------------
# Round-trip: the exact sequence the frontend performs
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_round_trip_notification_then_history(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Seed one event, GET /notifications sees it unread, POST /mark-read
    marks it, GET /notifications reports unread_count == 0 with the item's
    read now True, and GET /notifications/history/{entry_id} still returns
    the same event."""
    user = await _seed_user(db_session)
    entry = await _seed_entry(db_session, user, ticker="NFLX")
    rule = await _seed_rule(db_session, entry)
    await db_session.commit()

    event = await _seed_event(db_session, rule.id, "round-trip", _BASE_TIME, read=False)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        first_list = await client.get(NOTIFICATIONS_URL)
        assert first_list.json()["unread_count"] == 1
        assert first_list.json()["notifications"][0]["read"] is False

        mark_resp = await client.post(MARK_READ_URL)
        assert mark_resp.json() == {"marked": 1}

        second_list = await client.get(NOTIFICATIONS_URL)
        assert second_list.json()["unread_count"] == 0
        assert second_list.json()["notifications"][0]["read"] is True

        history_resp = await client.get(_history_url(str(entry.id)))

    assert history_resp.status_code == 200, history_resp.text
    assert {e["id"] for e in history_resp.json()["events"]} == {str(event.id)}
