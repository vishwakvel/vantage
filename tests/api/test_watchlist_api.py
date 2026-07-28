"""Watchlist and alert-rule API endpoint tests — the five routes backing
Phase 10 (10-07-PLAN.md, WATCH-01, WATCH-02, WATCH-03, WATCH-04, WATCH-05,
WATCH-08).

Coverage (Task 1 — watchlist entry routes, WATCH-01/WATCH-02):
  POST /watchlist:
    - happy path for a ticker with NO pre-existing Company row (D-01
      FK-upsert regression test — the single most important assertion in
      this module)
    - lowercase ticker normalisation
    - idempotence (D-04)
    - two different users watching the same ticker
    - malformed ticker rejection (400, no row written)
    - unauthenticated request (401/403, no row written)
  GET /watchlist:
    - empty state (200, never 404)
    - only the caller's own entries
    - D-13 latest-memo status with research / without research / cross-user
      isolation (T-10-05-MEMOLEAK)
    - nested alert_rules shape
    - unauthenticated request
  DELETE /watchlist/{entry_id}:
    - happy path (204, row gone)
    - D-03 cascade (alert_rules removed with the parent entry)
    - another user's entry_id (404, row untouched — T-10-05-IDOR)
    - unknown random UUID (404, same status as not-owned)
    - non-UUID path segment (422, not 500)
    - unauthenticated request

Coverage (Task 2 — alert-rule routes, WATCH-03/04/05/08):
  POST /watchlist/{entry_id}/rules:
    - NEW_FILING happy path (WATCH-03, D-12 empty config, enabled defaults true)
    - PRICE_MOVE happy path (WATCH-04, D-09 — persisted config normalises an
      integer threshold to a float)
    - SCHEDULED happy path for daily/weekly/monthly (WATCH-05, D-11)
    - D-06: two PRICE_MOVE rules with different thresholds on one entry
      both succeed with different ids
    - invalid config rejection matrix (400, unchanged row count) for an
      unknown direction, a zero threshold, an above-100 threshold, a
      missing direction, an extra key, an unknown cadence, a cron key, and
      a non-empty NEW_FILING config
    - an unknown rule_type string (422, no row written)
    - other-user entry_id (404, T-10-05-IDOR) and unknown entry UUID (404)
    - unauthenticated request
  PATCH /watchlist/rules/{rule_id}:
    - disable then re-enable, asserting the row still exists after being
      disabled (WATCH-08, D-07)
    - rule_type/config unchanged after a toggle
    - other-user rule_id (404, enabled value unchanged — join-through-parent
      ownership path)
    - unknown rule UUID (404), non-UUID segment (422)
    - unauthenticated request
  Round-trip: add a ticker, create one rule of each type, disable one, then
    a single GET /watchlist asserts the entry, all three rules, their
    enabled values, and the D-13 status fields — the shape the frontend
    actually consumes.

Every test drives the composed app via ``create_app()`` (through
``_make_authed_client``/``_make_unauthed_client``), so an unregistered
watchlist router would fail every assertion in this module with a 404.

Reuses ``_make_authed_client``, ``_make_unauthed_client``, ``_seed_user``,
``_seed_company``, and ``_seed_research_plan`` from
``tests.api.test_research_api``, and ``_seed_memo`` from
``tests.api.test_memo_routes`` (needed for the D-13 latest-status
assertions, since a memo requires a full ResearchRequest/ResearchPlan FK
chain). Skips automatically when test-postgres (port 5433) is unreachable,
via the ``db_session`` fixture's built-in skip behavior.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import AlertRule, Company, ResearchMemoStatus, WatchlistEntry
from tests.api.test_memo_routes import _seed_memo
from tests.api.test_research_api import (
    _make_authed_client,
    _make_unauthed_client,
    _seed_company,
    _seed_research_plan,
    _seed_user,
)

WATCHLIST_URL = "/api/v1/watchlist"


# ---------------------------------------------------------------------------
# POST /watchlist (WATCH-01, D-01, D-02, D-04)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_add_watchlist_entry_creates_company_row_for_new_ticker(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Adding a ticker with NO pre-existing Company row succeeds and upserts
    one (D-01 / PROJECT.md Company-row-upsert-gap regression test). The
    ticker is deliberately NOT seeded first — the point is that the handler
    creates the Company row, not the test.
    """
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.post(WATCHLIST_URL, json={"ticker": "MSFT"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticker"] == "MSFT"
    assert body["alert_rules"] == []
    assert body["latest_memo_status"] is None
    assert body["latest_memo_date"] is None

    result = await db_session.execute(select(Company).where(Company.ticker == "MSFT"))
    assert result.scalar_one_or_none() is not None


@pytest.mark.anyio
async def test_add_watchlist_entry_lowercase_ticker_normalised(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A lowercase ticker is normalised to uppercase and creates exactly one row."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.post(WATCHLIST_URL, json={"ticker": "nvda"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["ticker"] == "NVDA"

    result = await db_session.execute(
        select(WatchlistEntry).where(
            WatchlistEntry.user_id == user.id, WatchlistEntry.ticker == "NVDA"
        )
    )
    assert len(result.scalars().all()) == 1


@pytest.mark.anyio
async def test_add_watchlist_entry_idempotent(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Posting the same ticker twice returns 200 both times with the same
    entry id, and exactly one row exists (D-04)."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        first = await client.post(WATCHLIST_URL, json={"ticker": "GE"})
        second = await client.post(WATCHLIST_URL, json={"ticker": "GE"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] == second.json()["id"]

    result = await db_session.execute(
        select(WatchlistEntry).where(
            WatchlistEntry.user_id == user.id, WatchlistEntry.ticker == "GE"
        )
    )
    assert len(result.scalars().all()) == 1


@pytest.mark.anyio
async def test_add_watchlist_entry_two_users_same_ticker(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Two different users may each watch the same ticker with different rows."""
    user_a = await _seed_user(db_session)
    user_b = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user_a) as client:
        resp_a = await client.post(WATCHLIST_URL, json={"ticker": "KO"})

    async with _make_authed_client(db_session, test_settings, user_b) as client:
        resp_b = await client.post(WATCHLIST_URL, json={"ticker": "KO"})

    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text
    assert resp_a.json()["id"] != resp_b.json()["id"]


@pytest.mark.parametrize(
    "bad_ticker",
    ["", "   ", "AA PL", "AA-PL", "ABCDEFGHIJK"],
    ids=["empty", "whitespace_only", "space_in_middle", "punctuation", "eleven_chars"],
)
@pytest.mark.anyio
async def test_add_watchlist_entry_malformed_ticker_rejected(
    db_session: AsyncSession, test_settings: Settings, bad_ticker: str
) -> None:
    """A malformed ticker is rejected with 400 and writes no row."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.post(WATCHLIST_URL, json={"ticker": bad_ticker})

    assert resp.status_code == 400, resp.text

    result = await db_session.execute(
        select(WatchlistEntry).where(WatchlistEntry.user_id == user.id)
    )
    assert result.scalars().all() == []


@pytest.mark.anyio
async def test_add_watchlist_entry_requires_auth(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """An unauthenticated add-ticker request returns 401/403 and writes no row."""
    async with _make_unauthed_client(db_session, test_settings) as client:
        resp = await client.post(WATCHLIST_URL, json={"ticker": "SBUX"})

    assert resp.status_code in (401, 403), resp.text

    result = await db_session.execute(
        select(WatchlistEntry).where(WatchlistEntry.ticker == "SBUX")
    )
    assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# GET /watchlist (WATCH-02, D-13)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_watchlist_empty_returns_empty_list(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A user with no entries gets 200 and an empty entries list, never 404."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.get(WATCHLIST_URL)

    assert resp.status_code == 200, resp.text
    assert resp.json()["entries"] == []


@pytest.mark.anyio
async def test_list_watchlist_returns_only_callers_own_entries(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A second user's watchlisted ticker never appears in the caller's response."""
    user = await _seed_user(db_session)
    other_user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        await client.post(WATCHLIST_URL, json={"ticker": "AAPL"})

    async with _make_authed_client(db_session, test_settings, other_user) as client:
        await client.post(WATCHLIST_URL, json={"ticker": "TSLA"})

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.get(WATCHLIST_URL)

    assert resp.status_code == 200, resp.text
    tickers = {entry["ticker"] for entry in resp.json()["entries"]}
    assert tickers == {"AAPL"}


@pytest.mark.anyio
async def test_list_watchlist_with_research_shows_latest_status(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A watchlisted ticker with an owned memo shows that memo's status/date (D-13)."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["AAPL"])
    await db_session.commit()
    memo = await _seed_memo(
        db_session, plan, user, ticker="AAPL", status=ResearchMemoStatus.COMPLETE
    )

    async with _make_authed_client(db_session, test_settings, user) as client:
        await client.post(WATCHLIST_URL, json={"ticker": "AAPL"})
        resp = await client.get(WATCHLIST_URL)

    assert resp.status_code == 200, resp.text
    entry = resp.json()["entries"][0]
    assert entry["latest_memo_status"] == "COMPLETE"
    assert entry["latest_memo_date"].startswith(memo.created_at.date().isoformat())


@pytest.mark.anyio
async def test_list_watchlist_without_research_shows_null_status(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A watchlisted ticker with no memo returns null for both latest_memo_* fields."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        await client.post(WATCHLIST_URL, json={"ticker": "NFLX"})
        resp = await client.get(WATCHLIST_URL)

    entry = resp.json()["entries"][0]
    assert entry["latest_memo_status"] is None
    assert entry["latest_memo_date"] is None


@pytest.mark.anyio
async def test_list_watchlist_cross_user_memo_isolation(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A memo for the same ticker owned by a DIFFERENT user must not leak into
    the caller's latest-memo fields (T-10-05-MEMOLEAK regression test)."""
    user = await _seed_user(db_session)
    other_user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="AAPL")
    other_plan = await _seed_research_plan(
        db_session, other_user, resolved_tickers=["AAPL"]
    )
    await db_session.commit()
    await _seed_memo(
        db_session,
        other_plan,
        other_user,
        ticker="AAPL",
        status=ResearchMemoStatus.COMPLETE,
    )

    async with _make_authed_client(db_session, test_settings, user) as client:
        await client.post(WATCHLIST_URL, json={"ticker": "AAPL"})
        resp = await client.get(WATCHLIST_URL)

    entry = resp.json()["entries"][0]
    assert entry["latest_memo_status"] is None
    assert entry["latest_memo_date"] is None


@pytest.mark.anyio
async def test_list_watchlist_nested_rules(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """An entry with two rules returns both inside that entry's alert_rules list."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        add_resp = await client.post(WATCHLIST_URL, json={"ticker": "IBM"})
        entry_id = add_resp.json()["id"]
        await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "NEW_FILING", "config": {}},
        )
        await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={
                "rule_type": "PRICE_MOVE",
                "config": {"threshold_pct": 5, "direction": "down"},
            },
        )
        resp = await client.get(WATCHLIST_URL)

    entry = resp.json()["entries"][0]
    assert len(entry["alert_rules"]) == 2
    for rule in entry["alert_rules"]:
        assert set(rule.keys()) == {
            "id",
            "rule_type",
            "config",
            "enabled",
            "created_at",
        }


@pytest.mark.anyio
async def test_list_watchlist_requires_auth(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """An unauthenticated GET /watchlist returns 401/403."""
    async with _make_unauthed_client(db_session, test_settings) as client:
        resp = await client.get(WATCHLIST_URL)

    assert resp.status_code in (401, 403), resp.text


# ---------------------------------------------------------------------------
# DELETE /watchlist/{entry_id} (WATCH-01, D-03)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_delete_watchlist_entry_happy_path(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Deleting an owned entry returns 204 with no body and removes the row."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        add_resp = await client.post(WATCHLIST_URL, json={"ticker": "ORCL"})
        entry_id = add_resp.json()["id"]
        del_resp = await client.delete(f"{WATCHLIST_URL}/{entry_id}")

    assert del_resp.status_code == 204, del_resp.text
    assert del_resp.content == b""

    result = await db_session.execute(
        select(WatchlistEntry).where(WatchlistEntry.id == uuid.UUID(entry_id))
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.anyio
async def test_delete_watchlist_entry_cascades_alert_rules(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Deleting an entry with two alert rules removes both rule rows (D-03)."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        add_resp = await client.post(WATCHLIST_URL, json={"ticker": "CRM"})
        entry_id = add_resp.json()["id"]
        rule1 = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "NEW_FILING", "config": {}},
        )
        rule2 = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "SCHEDULED", "config": {"cadence": "daily"}},
        )
        rule_ids = [uuid.UUID(rule1.json()["id"]), uuid.UUID(rule2.json()["id"])]

        del_resp = await client.delete(f"{WATCHLIST_URL}/{entry_id}")

    assert del_resp.status_code == 204, del_resp.text

    result = await db_session.execute(select(AlertRule).where(AlertRule.id.in_(rule_ids)))
    assert result.scalars().all() == []


@pytest.mark.anyio
async def test_delete_watchlist_entry_other_user_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Another user's entry id returns 404 and the row still exists afterwards
    (T-10-05-IDOR)."""
    owner = await _seed_user(db_session)
    other_user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, owner) as client:
        add_resp = await client.post(WATCHLIST_URL, json={"ticker": "AMD"})
        entry_id = add_resp.json()["id"]

    async with _make_authed_client(db_session, test_settings, other_user) as client:
        del_resp = await client.delete(f"{WATCHLIST_URL}/{entry_id}")

    assert del_resp.status_code == 404, del_resp.text

    result = await db_session.execute(
        select(WatchlistEntry).where(WatchlistEntry.id == uuid.UUID(entry_id))
    )
    assert result.scalar_one_or_none() is not None


@pytest.mark.anyio
async def test_delete_watchlist_entry_unknown_uuid_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """An unknown random UUID returns 404 — same status as the not-owned case."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.delete(f"{WATCHLIST_URL}/{uuid.uuid4()}")

    assert resp.status_code == 404, resp.text


@pytest.mark.anyio
async def test_delete_watchlist_entry_non_uuid_returns_422(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A non-UUID path segment returns 422, not 500."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.delete(f"{WATCHLIST_URL}/not-a-uuid")

    assert resp.status_code == 422, resp.text


@pytest.mark.anyio
async def test_delete_watchlist_entry_requires_auth(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """An unauthenticated delete returns 401/403 and deletes nothing."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        add_resp = await client.post(WATCHLIST_URL, json={"ticker": "PYPL"})
        entry_id = add_resp.json()["id"]

    async with _make_unauthed_client(db_session, test_settings) as client:
        resp = await client.delete(f"{WATCHLIST_URL}/{entry_id}")

    assert resp.status_code in (401, 403), resp.text

    result = await db_session.execute(
        select(WatchlistEntry).where(WatchlistEntry.id == uuid.UUID(entry_id))
    )
    assert result.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Task 2 helper — seed a watchlist entry through the API (WATCH-03/04/05/08)
# ---------------------------------------------------------------------------


async def _add_ticker(client, ticker: str = "AAPL") -> str:
    """POST /watchlist for *ticker* and return the new entry's id as a string.

    Every rule test starts from a real entry created through the same API
    surface being tested, matching Task 1's seeding style rather than
    inserting a WatchlistEntry row directly.
    """
    resp = await client.post(WATCHLIST_URL, json={"ticker": ticker})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# POST /watchlist/{entry_id}/rules (WATCH-03, WATCH-04, WATCH-05, D-06, D-09,
# D-11, D-12)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_alert_rule_new_filing_happy_path(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A NEW_FILING rule persists an empty config with enabled defaulting true (WATCH-03)."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        entry_id = await _add_ticker(client, "BA")
        resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "NEW_FILING", "config": {}},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rule_type"] == "NEW_FILING"
    assert body["config"] == {}
    assert body["enabled"] is True

    result = await db_session.execute(
        select(AlertRule).where(AlertRule.id == uuid.UUID(body["id"]))
    )
    rule = result.scalar_one()
    assert rule.watchlist_id == uuid.UUID(entry_id)


@pytest.mark.anyio
async def test_create_alert_rule_price_move_happy_path(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A PRICE_MOVE rule persists threshold_pct as a float (WATCH-04, D-09)."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        entry_id = await _add_ticker(client, "F")
        resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={
                "rule_type": "PRICE_MOVE",
                "config": {"threshold_pct": 5, "direction": "down"},
            },
        )

    assert resp.status_code == 200, resp.text

    result = await db_session.execute(
        select(AlertRule).where(AlertRule.id == uuid.UUID(resp.json()["id"]))
    )
    rule = result.scalar_one()
    assert rule.config["threshold_pct"] == 5.0
    assert isinstance(rule.config["threshold_pct"], float)
    assert rule.config["direction"] == "down"


@pytest.mark.parametrize("cadence", ["daily", "weekly", "monthly"])
@pytest.mark.anyio
async def test_create_alert_rule_scheduled_happy_path(
    db_session: AsyncSession, test_settings: Settings, cadence: str
) -> None:
    """A SCHEDULED rule persists the given cadence (WATCH-05, D-11)."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        entry_id = await _add_ticker(client, "GM")
        resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "SCHEDULED", "config": {"cadence": cadence}},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["config"]["cadence"] == cadence

    result = await db_session.execute(
        select(AlertRule).where(AlertRule.id == uuid.UUID(body["id"]))
    )
    rule = result.scalar_one()
    assert rule.config["cadence"] == cadence


@pytest.mark.anyio
async def test_create_alert_rule_multiple_same_type_allowed(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Two PRICE_MOVE rules on the same entry both succeed with different
    ids, and GET /watchlist shows both (D-06)."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        entry_id = await _add_ticker(client, "DIS")
        first = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={
                "rule_type": "PRICE_MOVE",
                "config": {"threshold_pct": 5, "direction": "up"},
            },
        )
        second = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={
                "rule_type": "PRICE_MOVE",
                "config": {"threshold_pct": 15, "direction": "up"},
            },
        )
        list_resp = await client.get(WATCHLIST_URL)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] != second.json()["id"]

    entry = next(e for e in list_resp.json()["entries"] if e["id"] == entry_id)
    assert len(entry["alert_rules"]) == 2


@pytest.mark.parametrize(
    "rule_type,config",
    [
        ("PRICE_MOVE", {"threshold_pct": 5, "direction": "sideways"}),
        ("PRICE_MOVE", {"threshold_pct": 0, "direction": "up"}),
        ("PRICE_MOVE", {"threshold_pct": 101, "direction": "up"}),
        ("PRICE_MOVE", {"threshold_pct": 5}),
        ("PRICE_MOVE", {"threshold_pct": 5, "direction": "up", "extra": "nope"}),
        ("SCHEDULED", {"cadence": "hourly"}),
        ("SCHEDULED", {"cron": "* * * * *"}),
        ("NEW_FILING", {"anything": "here"}),
    ],
    ids=[
        "unknown_direction",
        "zero_threshold",
        "above_100_threshold",
        "missing_direction",
        "extra_key",
        "unknown_cadence",
        "cron_key",
        "non_empty_new_filing",
    ],
)
@pytest.mark.anyio
async def test_create_alert_rule_invalid_config_rejected(
    db_session: AsyncSession,
    test_settings: Settings,
    rule_type: str,
    config: dict,
) -> None:
    """An invalid rule config is rejected with 400 and writes no row."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        entry_id = await _add_ticker(client, "COST")
        resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": rule_type, "config": config},
        )

    assert resp.status_code == 400, resp.text

    result = await db_session.execute(
        select(AlertRule).where(AlertRule.watchlist_id == uuid.UUID(entry_id))
    )
    assert result.scalars().all() == []


@pytest.mark.anyio
async def test_create_alert_rule_unknown_rule_type_returns_422(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """An unknown rule_type string is rejected by Pydantic with 422 before
    the handler body runs."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        entry_id = await _add_ticker(client, "NKE")
        resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "SENTIMENT_SPIKE", "config": {}},
        )

    assert resp.status_code == 422, resp.text

    result = await db_session.execute(
        select(AlertRule).where(AlertRule.watchlist_id == uuid.UUID(entry_id))
    )
    assert result.scalars().all() == []


@pytest.mark.anyio
async def test_create_alert_rule_other_user_entry_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Another user's entry_id returns 404 and writes no row (T-10-05-IDOR)."""
    owner = await _seed_user(db_session)
    other_user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, owner) as client:
        entry_id = await _add_ticker(client, "PEP")

    async with _make_authed_client(db_session, test_settings, other_user) as client:
        resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "NEW_FILING", "config": {}},
        )

    assert resp.status_code == 404, resp.text

    result = await db_session.execute(
        select(AlertRule).where(AlertRule.watchlist_id == uuid.UUID(entry_id))
    )
    assert result.scalars().all() == []


@pytest.mark.anyio
async def test_create_alert_rule_unknown_entry_uuid_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """An unknown random entry UUID returns 404."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.post(
            f"{WATCHLIST_URL}/{uuid.uuid4()}/rules",
            json={"rule_type": "NEW_FILING", "config": {}},
        )

    assert resp.status_code == 404, resp.text


@pytest.mark.anyio
async def test_create_alert_rule_requires_auth(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """An unauthenticated create-rule request returns 401/403 and writes no row."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        entry_id = await _add_ticker(client, "V")

    async with _make_unauthed_client(db_session, test_settings) as client:
        resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "NEW_FILING", "config": {}},
        )

    assert resp.status_code in (401, 403), resp.text

    result = await db_session.execute(
        select(AlertRule).where(AlertRule.watchlist_id == uuid.UUID(entry_id))
    )
    assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# PATCH /watchlist/rules/{rule_id} (WATCH-08, D-07)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_toggle_alert_rule_disable_then_reenable(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Disabling a rule leaves the row in place (D-07); re-enabling flips it back."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        entry_id = await _add_ticker(client, "MA")
        create_resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "NEW_FILING", "config": {}},
        )
        rule_id = create_resp.json()["id"]

        disable_resp = await client.patch(
            f"{WATCHLIST_URL}/rules/{rule_id}", json={"enabled": False}
        )
        assert disable_resp.status_code == 200, disable_resp.text
        assert disable_resp.json()["enabled"] is False

        after_disable = await db_session.execute(
            select(AlertRule).where(AlertRule.id == uuid.UUID(rule_id))
        )
        disabled_rule = after_disable.scalar_one_or_none()
        assert disabled_rule is not None, "rule row must still exist after disabling (D-07)"
        assert disabled_rule.enabled is False

        reenable_resp = await client.patch(
            f"{WATCHLIST_URL}/rules/{rule_id}", json={"enabled": True}
        )
        assert reenable_resp.status_code == 200, reenable_resp.text
        assert reenable_resp.json()["enabled"] is True

    after_reenable = await db_session.execute(
        select(AlertRule).where(AlertRule.id == uuid.UUID(rule_id))
    )
    assert after_reenable.scalar_one().enabled is True


@pytest.mark.anyio
async def test_toggle_alert_rule_type_and_config_unchanged(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A toggle only flips enabled — rule_type and config are unchanged."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        entry_id = await _add_ticker(client, "JPM")
        create_resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "SCHEDULED", "config": {"cadence": "weekly"}},
        )
        rule_id = create_resp.json()["id"]

        toggle_resp = await client.patch(
            f"{WATCHLIST_URL}/rules/{rule_id}", json={"enabled": False}
        )

    assert toggle_resp.status_code == 200, toggle_resp.text
    body = toggle_resp.json()
    assert body["rule_type"] == "SCHEDULED"
    assert body["config"] == {"cadence": "weekly"}


@pytest.mark.anyio
async def test_toggle_alert_rule_other_user_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Another user's rule_id returns 404 and its enabled value is unchanged
    (exercises the join-through-parent ownership path)."""
    owner = await _seed_user(db_session)
    other_user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, owner) as client:
        entry_id = await _add_ticker(client, "WMT")
        create_resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "NEW_FILING", "config": {}},
        )
        rule_id = create_resp.json()["id"]

    async with _make_authed_client(db_session, test_settings, other_user) as client:
        resp = await client.patch(
            f"{WATCHLIST_URL}/rules/{rule_id}", json={"enabled": False}
        )

    assert resp.status_code == 404, resp.text

    result = await db_session.execute(
        select(AlertRule).where(AlertRule.id == uuid.UUID(rule_id))
    )
    assert result.scalar_one().enabled is True


@pytest.mark.anyio
async def test_toggle_alert_rule_unknown_uuid_returns_404(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """An unknown random rule UUID returns 404."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.patch(
            f"{WATCHLIST_URL}/rules/{uuid.uuid4()}", json={"enabled": False}
        )

    assert resp.status_code == 404, resp.text


@pytest.mark.anyio
async def test_toggle_alert_rule_non_uuid_returns_422(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """A non-UUID rule_id path segment returns 422."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        resp = await client.patch(
            f"{WATCHLIST_URL}/rules/not-a-uuid", json={"enabled": False}
        )

    assert resp.status_code == 422, resp.text


@pytest.mark.anyio
async def test_toggle_alert_rule_requires_auth(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """An unauthenticated toggle returns 401/403 and changes nothing."""
    user = await _seed_user(db_session)
    await db_session.commit()

    async with _make_authed_client(db_session, test_settings, user) as client:
        entry_id = await _add_ticker(client, "XOM")
        create_resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "NEW_FILING", "config": {}},
        )
        rule_id = create_resp.json()["id"]

    async with _make_unauthed_client(db_session, test_settings) as client:
        resp = await client.patch(
            f"{WATCHLIST_URL}/rules/{rule_id}", json={"enabled": False}
        )

    assert resp.status_code in (401, 403), resp.text

    result = await db_session.execute(
        select(AlertRule).where(AlertRule.id == uuid.UUID(rule_id))
    )
    assert result.scalar_one().enabled is True


# ---------------------------------------------------------------------------
# Round-trip: the shape the frontend actually consumes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_watchlist_round_trip_full_shape(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """Add a ticker, create one rule of each type, disable one, then a
    single GET /watchlist carries the entry, all three rules, their
    enabled values, and the D-13 status fields."""
    user = await _seed_user(db_session)
    await _seed_company(db_session, ticker="INTC")
    plan = await _seed_research_plan(db_session, user, resolved_tickers=["INTC"])
    await db_session.commit()
    memo = await _seed_memo(
        db_session, plan, user, ticker="INTC", status=ResearchMemoStatus.PARTIAL
    )

    async with _make_authed_client(db_session, test_settings, user) as client:
        entry_id = await _add_ticker(client, "INTC")

        filing_resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "NEW_FILING", "config": {}},
        )
        assert filing_resp.status_code == 200, filing_resp.text

        price_resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={
                "rule_type": "PRICE_MOVE",
                "config": {"threshold_pct": 10, "direction": "either"},
            },
        )
        assert price_resp.status_code == 200, price_resp.text

        scheduled_resp = await client.post(
            f"{WATCHLIST_URL}/{entry_id}/rules",
            json={"rule_type": "SCHEDULED", "config": {"cadence": "monthly"}},
        )
        assert scheduled_resp.status_code == 200, scheduled_resp.text
        disabled_rule_id = scheduled_resp.json()["id"]

        await client.patch(
            f"{WATCHLIST_URL}/rules/{disabled_rule_id}", json={"enabled": False}
        )

        resp = await client.get(WATCHLIST_URL)

    assert resp.status_code == 200, resp.text
    entry = next(e for e in resp.json()["entries"] if e["id"] == entry_id)
    assert entry["latest_memo_status"] == "PARTIAL"
    assert entry["latest_memo_date"].startswith(memo.created_at.date().isoformat())

    rules_by_type = {r["rule_type"]: r for r in entry["alert_rules"]}
    assert set(rules_by_type) == {"NEW_FILING", "PRICE_MOVE", "SCHEDULED"}
    assert rules_by_type["NEW_FILING"]["enabled"] is True
    assert rules_by_type["PRICE_MOVE"]["enabled"] is True
    assert rules_by_type["SCHEDULED"]["enabled"] is False
