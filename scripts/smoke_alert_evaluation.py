"""Live end-to-end smoke test for Phase 11 alert evaluation (WATCH-06/07/09).

This is a standalone async script — NOT a pytest test, so it never runs in
CI and never touches real EDGAR or real market data from an automated run
(T-11-11-CIEDGAR). It proves the three links no unit or integration test in
this project can cover, because they all either mock the external service or
run everything inside one process:

1. A real Redis pub/sub hop between two OS processes — the Celery beat
   worker publishes on ``notifications:{user_id}`` and the FastAPI
   ``/ws/notifications`` route subscribes to it. Here, this script itself
   plays the subscriber role, proving the wire format end-to-end.
2. A real EDGAR full-text-search (EFTS) response shape, via
   ``evaluate_all_rules``'s NEW_FILING path.
3. A real market-data (yfinance) response shape, via
   ``evaluate_all_rules``'s PRICE_MOVE path.

Prerequisites:
    - The dev docker-compose stack's ``postgres`` and ``redis`` services
      must be running (``docker-compose up -d postgres redis``).
    - ``.env`` must be populated (``DATABASE_URL``, ``REDIS_URL``,
      ``JWT_SECRET_KEY``, ``GROQ_API_KEY`` — required by ``Settings``even
      though this script never calls Groq).
    - Alembic migrations must be at head (``alembic upgrade head``).

Invocation:
    python scripts/smoke_alert_evaluation.py --ticker AAPL
    python scripts/smoke_alert_evaluation.py --ticker AAPL --keep   # skip teardown

This script is deliberately NOT collected by pytest, NOT imported from any
application module, and NOT referenced from any CI configuration
(T-11-11-CIEDGAR) — asserted by this plan's own acceptance-criteria greps.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import (
    AlertEvent,
    AlertRule,
    AlertRuleType,
    User,
    WatchlistEntry,
)
from app.db.session import session_scope
from app.services.alert_evaluation_service import evaluate_all_rules
from app.services.company_service import ensure_company_exists
from app.services.notification_publisher import notification_channel

#: Clearly non-production email so this row is never mistaken for a real
#: user (T-11-11-SMOKEDATA) — the ``.invalid`` TLD is reserved by RFC 2606
#: and can never resolve to a real mailbox.
SMOKE_EMAIL = "smoke-test-alert-evaluation@vantage.invalid"
SMOKE_PASSWORD = "smoke-test-password-not-a-real-secret"  # noqa: S105 - throwaway

#: The six-key notification payload contract from
#: ``notification_publisher.notification_payload`` plus the ``"type"``
#: envelope key ``publish_notification`` adds — this is the ONE place the
#: expected key set is declared for this script's own assertion.
EXPECTED_PAYLOAD_KEYS = {"type", "id", "alert_rule_id", "message", "triggered_at", "read"}

#: How long to let the background Redis listener catch up with a publish
#: before this script asserts on what it has collected so far. Generous
#: relative to a loopback pub/sub round trip, cheap relative to a human
#: waiting for the script to finish.
_PUBSUB_SETTLE_SECONDS = 1.5


class SmokeTestFailure(AssertionError):
    """Raised when a live assertion fails — caught once at the top level."""


async def _get_or_create_smoke_user(session: AsyncSession) -> User:
    """Reuse the smoke user by email if a prior run left one, else create it."""
    result = await session.execute(select(User).where(User.email == SMOKE_EMAIL))
    user = result.scalar_one_or_none()
    if user is not None:
        return user
    user = User(email=SMOKE_EMAIL, password_hash=hash_password(SMOKE_PASSWORD))
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def _reset_leftover_entry(session: AsyncSession, user: User, ticker: str) -> None:
    """Delete a leftover WatchlistEntry from a prior ``--keep`` run.

    Keeps the script idempotent regardless of whether the previous
    invocation was run with ``--keep`` and never cleaned up: the unique
    ``(user_id, ticker)`` constraint on ``WatchlistEntry`` would otherwise
    reject this run's fresh insert.
    """
    result = await session.execute(
        select(WatchlistEntry).where(
            WatchlistEntry.user_id == user.id, WatchlistEntry.ticker == ticker
        )
    )
    entry = result.scalar_one_or_none()
    if entry is not None:
        await session.delete(entry)
        await session.commit()


async def _collect_pubsub_messages(pubsub: aioredis.client.PubSub, sink: list[dict]) -> None:
    """Background task: append every real (non-subscribe-ack) message to *sink*."""
    async for message in pubsub.listen():
        if message.get("type") == "message":
            sink.append(json.loads(message["data"]))


def _check(condition: bool, description: str) -> None:
    """Assert-and-print helper — raises SmokeTestFailure with context on failure."""
    if not condition:
        raise SmokeTestFailure(f"FAILED: {description}")
    print(f"  [OK] {description}")


async def run_smoke_test(ticker: str, keep: bool) -> None:
    settings = get_settings()
    ticker = ticker.upper()

    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = redis_client.pubsub()
    collected: list[dict] = []
    listener_task: asyncio.Task | None = None
    captured_event_ids: list[str] = []

    try:
        # --- Step 1: seed -----------------------------------------------
        print(f"\n=== Step 1: seed (ticker={ticker}) ===")
        async with session_scope() as session:
            user = await _get_or_create_smoke_user(session)
            await session.commit()
            await _reset_leftover_entry(session, user, ticker)

            await ensure_company_exists(ticker, session)

            entry = WatchlistEntry(user_id=user.id, ticker=ticker)
            session.add(entry)
            await session.flush()

            two_days_ago = datetime.now(UTC) - timedelta(days=2)

            rule_new_filing = AlertRule(
                watchlist_id=entry.id,
                rule_type=AlertRuleType.NEW_FILING,
                config={},
            )
            rule_price_move = AlertRule(
                watchlist_id=entry.id,
                rule_type=AlertRuleType.PRICE_MOVE,
                config={"threshold_pct": 0.01, "direction": "either"},
            )
            rule_scheduled = AlertRule(
                watchlist_id=entry.id,
                rule_type=AlertRuleType.SCHEDULED,
                config={"cadence": "daily"},
                created_at=two_days_ago,
            )
            session.add_all([rule_new_filing, rule_price_move, rule_scheduled])
            await session.commit()
            await session.refresh(rule_new_filing)
            await session.refresh(rule_price_move)
            await session.refresh(rule_scheduled)

            print(f"  Seeded user={user.id} entry={entry.id}")
            print(
                "  Seeded rules: "
                f"NEW_FILING={rule_new_filing.id} "
                f"PRICE_MOVE={rule_price_move.id} "
                f"SCHEDULED={rule_scheduled.id}"
            )

            user_id = user.id

            # --- Step 2: subscribe ---------------------------------------
            print("\n=== Step 2: subscribe to the real Redis notification channel ===")
            channel = notification_channel(str(user_id))
            await pubsub.subscribe(channel)
            listener_task = asyncio.create_task(_collect_pubsub_messages(pubsub, collected))
            print(f"  Subscribed to channel: {channel}")

            # --- Step 3: tick one ------------------------------------------
            print("\n=== Step 3: tick one — evaluate_all_rules(session) ===")
            fired_tick_one = await evaluate_all_rules(session, settings=settings)
            await asyncio.sleep(_PUBSUB_SETTLE_SECONDS)

            _check(fired_tick_one == 1, f"tick one fired exactly 1 rule (got {fired_tick_one})")
            _check(
                rule_scheduled.state is not None
                and rule_scheduled.state.get("last_triggered_at") is not None,
                "SCHEDULED rule fired on tick one",
            )

            accession = (rule_new_filing.state or {}).get("last_seen_accession")
            _check(
                rule_new_filing.state is not None and accession is not None,
                "NEW_FILING rule seeded its cursor (did NOT fire) on tick one",
            )
            print(
                "  NEW_FILING seeded accession (eyeball this — real SEC format, e.g. "
                f"0001234567-25-000123): {accession!r}"
            )

            baseline_price = (rule_price_move.state or {}).get("last_price")
            _check(
                isinstance(baseline_price, int | float) and baseline_price > 0,
                "PRICE_MOVE rule seeded a plausible baseline price (did NOT fire) on tick one",
            )
            print(
                "  PRICE_MOVE seeded baseline price (eyeball this — should be a "
                f"plausible {ticker} share price): ${baseline_price:.2f}"
            )

            # --- Step 4: tick two --------------------------------------------
            print("\n=== Step 4: tick two — simulate a large PRICE_MOVE and re-evaluate ===")
            simulated_baseline = baseline_price / 2.0
            rule_price_move.state = {
                **(rule_price_move.state or {}),
                "last_price": simulated_baseline,
            }
            await session.flush()
            await session.commit()
            print(
                f"  Mutated PRICE_MOVE baseline to ${simulated_baseline:.2f} (half the "
                "observed price) to force a fire"
            )

            fired_tick_two = await evaluate_all_rules(session, settings=settings)
            await asyncio.sleep(_PUBSUB_SETTLE_SECONDS)

            _check(fired_tick_two == 1, f"tick two fired exactly 1 rule (got {fired_tick_two})")
            _check(
                rule_price_move.state is not None
                and rule_price_move.state.get("last_price") != simulated_baseline,
                "PRICE_MOVE rule fired and rolled its baseline forward on tick two",
            )
            _check(
                rule_scheduled.state is not None
                and rule_scheduled.state.get("last_triggered_at") is not None,
                "SCHEDULED rule did NOT refire on tick two (last_triggered_at still set "
                "from tick one)",
            )

            # --- Step 5: assert delivery -----------------------------------
            print("\n=== Step 5: assert delivery over the real Redis channel ===")
            _check(
                len(collected) == 2,
                "exactly 2 notification messages received over Redis pub/sub "
                f"(got {len(collected)})",
            )
            for i, payload in enumerate(collected, start=1):
                _check(
                    set(payload.keys()) == EXPECTED_PAYLOAD_KEYS,
                    f"message {i} payload key set is exactly {sorted(EXPECTED_PAYLOAD_KEYS)} "
                    f"(got {sorted(payload.keys())})",
                )
                captured_event_ids.append(payload["id"])
                print(
                    f"  Message {i} composed copy (eyeball this — read like a real "
                    f"user-facing sentence): {payload['message']!r}"
                )

            # --- Step 6: assert durability -----------------------------------
            print("\n=== Step 6: assert durability in a FRESH session ===")

        async with session_scope() as fresh_session:
            result = await fresh_session.execute(
                select(AlertEvent).where(
                    AlertEvent.alert_rule_id.in_([rule_scheduled.id, rule_price_move.id])
                )
            )
            durable_rows = result.scalars().all()
            _check(
                len(durable_rows) == len(collected),
                f"durable alert_events row count ({len(durable_rows)}) matches published "
                f"message count ({len(collected)})",
            )

        print("\n=== PASS SUMMARY ===")
        print(f"  ticker: {ticker}")
        print(f"  user_id: {user_id}")
        print(f"  fired tick one: {fired_tick_one}")
        print(f"  fired tick two: {fired_tick_two}")
        print(f"  messages received: {len(collected)}")
        print(f"  durable rows confirmed: {len(durable_rows)}")

    finally:
        if listener_task is not None:
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass
        await pubsub.aclose()
        await redis_client.aclose()

        # --- Step 7: teardown ---------------------------------------------
        if not keep:
            print("\n=== Step 7: teardown ===")
            async with session_scope() as session:
                result = await session.execute(
                    select(WatchlistEntry).where(
                        WatchlistEntry.user_id == user_id, WatchlistEntry.ticker == ticker
                    )
                )
                entry_to_delete = result.scalar_one_or_none()
                if entry_to_delete is not None:
                    await session.delete(entry_to_delete)
                    await session.commit()

                result = await session.execute(select(User).where(User.id == user_id))
                user_to_delete = result.scalar_one_or_none()
                if user_to_delete is not None:
                    await session.delete(user_to_delete)
                    await session.commit()

            async with session_scope() as verify_session:
                if captured_event_ids:
                    result = await verify_session.execute(
                        select(AlertEvent).where(AlertEvent.id.in_(captured_event_ids))
                    )
                    remaining = result.scalars().all()
                    _check(
                        len(remaining) == 0,
                        f"zero alert_events rows remain after cascade teardown "
                        f"(found {len(remaining)}) — D-06 cascade proven live",
                    )
            print("  Teardown complete: smoke WatchlistEntry + user deleted, cascade verified.")
        else:
            print("\n=== Step 7: teardown SKIPPED (--keep) ===")
            print(f"  Smoke user id: {user_id}  email: {SMOKE_EMAIL}")
            print(
                "  Log in as this user (or point your session at this user id) to inspect the UI."
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live smoke test for Phase 11 alert evaluation (WATCH-06/07/09)."
    )
    parser.add_argument("--ticker", default="AAPL", help="Ticker to exercise (default: AAPL).")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Skip teardown, leaving the seeded rows in place for manual browser inspection.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run_smoke_test(args.ticker, args.keep))
    except SmokeTestFailure as exc:
        print(f"\n=== FAIL ===\n{exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - top-level script boundary
        print(f"\n=== FAIL (unexpected error) ===\n{exc!r}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
