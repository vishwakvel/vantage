"""Alert-rule evaluation — the single implementation of WATCH-06 and WATCH-09.

``evaluate_all_rules(session)`` is the ONE entry point plan 11-05's Celery
periodic task calls. It queries every enabled ``AlertRule`` joined to its
owning ``WatchlistEntry`` in a single statement, dispatches each rule to a
per-type evaluator (NEW_FILING, PRICE_MOVE, SCHEDULED), updates the rule's
``state`` blob (D-03) whether or not it fired (D-05's structural
self-re-arming), persists an ``alert_events`` row when it fires (D-06),
commits, and only then publishes a live notification on the user's Redis
channel (D-09).

This module implements D-01 through D-05 from
``11-CONTEXT.md``:

- D-01 (non-goal, verbatim): a SCHEDULED rule fires a notification only,
  and never launches a research run. Auto-firing a multi-agent Groq run
  from an unattended cron job was considered and rejected on both
  requirements grounds (WATCH-06 asks for a notification, nothing more)
  and cost/quota grounds (the Groq free-tier daily quota is already
  exhausted by normal interactive use — see STATE.md).
- D-02: NEW_FILING's first-ever evaluation seeds its cursor without
  notifying.
- D-03: runtime evaluator state lives in ``AlertRule.state``, a nullable
  JSON blob this module owns exclusively.
- D-04: every enabled rule of all three types is evaluated in one pass.
- D-05: no rule type needs a separate re-arm/cooldown flag, because
  ``state`` is written on every tick regardless of whether the rule fired.

Ordering invariants (both structurally enforced, see acceptance criteria):
1. ``session.commit()`` always happens BEFORE ``publish_notification()`` —
   a WebSocket client that receives a push and immediately re-fetches must
   never observe a row that is not yet visible, and a rolled-back
   transaction must never have produced a push.
2. Every ``rule.state`` write reassigns a NEW dict
   (``rule.state = {**(rule.state or {}), ...}``) — a plain SQLAlchemy
   ``JSON`` column does not track in-place mutation, so mutating the
   existing dict would be silently discarded at commit.

This is a leaf service in the ``watchlist_service.py`` sense: a session is
passed in, there is no FastAPI or Celery import and no request context, so
it is directly unit-testable. It never imports Celery, FastAPI, the
research graph, the research Celery task, or Groq — structurally enforced
by an AST import audit in this plan's acceptance criteria (T-11-03-QUOTA).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.db.models import AlertEvent, AlertRule, AlertRuleType, WatchlistEntry
from app.services.edgar_client import edgar_client
from app.services.live_price_source import live_price_source
from app.services.notification_publisher import (
    notification_payload,
    publish_notification,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.config import Settings

logger = logging.getLogger(__name__)

#: D-11's fixed cadence presets, mapped to fixed intervals — no cron or
#: calendar arithmetic anywhere in this feature (Phase 10 D-11). "monthly"
#: is deliberately 30 days, not a calendar month: a fixed 30-day preset
#: keeps the interval arithmetic in `_evaluate_scheduled` simple and
#: unambiguous, matching Phase 10's fixed-presets-only convention. Keys
#: must be exactly the members of `watchlist_service.SCHEDULED_CADENCES`
#: (enforced by this plan's acceptance criteria).
CADENCE_INTERVALS: dict[str, timedelta] = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
}

#: D-02: the EFTS lookback window for NEW_FILING detection. Narrower than
#: ingestion_service.py's 3-year window (used to backfill a ticker's
#: history) because this query only needs to answer "what is the most
#: recent filing right now" — a smaller window means a smaller response
#: per rule per tick.
EFTS_LOOKBACK_DAYS: int = 90


async def evaluate_all_rules(
    session: "AsyncSession", settings: "Settings | None" = None
) -> int:
    """Evaluate every enabled ``AlertRule`` in one pass and return fired count.

    Issues exactly one query (no per-rule lookup), dispatches each row to
    its per-type evaluator inside an isolated try/except so one rule's
    failure can never abort the tick or corrupt another rule's state, and
    publishes a live notification only after the firing rule's row is
    durably committed (D-09).

    Args:
        session: Async DB session — this function opens no session of its
            own; the Celery task caller (plan 11-05) owns the session
            lifecycle via its own ``session_scope()``.
        settings: Optional ``Settings`` override, threaded through to the
            live-push call (test seam); defaults to ``get_settings()`` when
            not supplied.

    Returns:
        The number of rules that fired a notification this tick.
    """
    now = datetime.now(timezone.utc)

    result = await session.execute(
        select(AlertRule, WatchlistEntry.ticker, WatchlistEntry.user_id)
        .join(WatchlistEntry, AlertRule.watchlist_id == WatchlistEntry.id)
        .where(AlertRule.enabled.is_(True))
        .order_by(AlertRule.created_at.asc())
    )
    rows = result.all()

    fired = 0

    for rule, ticker, user_id in rows:
        try:
            if rule.rule_type is AlertRuleType.NEW_FILING:
                message = await _evaluate_new_filing(rule, ticker, now)
            elif rule.rule_type is AlertRuleType.PRICE_MOVE:
                message = await _evaluate_price_move(rule, ticker, now)
            elif rule.rule_type is AlertRuleType.SCHEDULED:
                message = _evaluate_scheduled(rule, ticker, now)
            else:
                # Enum drift: a rule_type this dispatcher doesn't know
                # about must fail loudly, mirroring
                # watchlist_service.validate_rule_config's
                # T-10-03-ENUMDRIFT discipline, rather than silently doing
                # nothing.
                logger.warning(
                    "Alert rule %s has unrecognised rule_type %s",
                    rule.id,
                    rule.rule_type,
                )
                continue

            event: AlertEvent | None = None
            if message is not None:
                event = AlertEvent(
                    alert_rule_id=rule.id, message=message, triggered_at=now
                )
                session.add(event)
                await session.flush()

            await session.commit()

            if event is not None:
                try:
                    payload = notification_payload(
                        event_id=str(event.id),
                        alert_rule_id=str(rule.id),
                        message=message,
                        triggered_at=now.isoformat(),
                        read=False,
                    )
                    await publish_notification(str(user_id), payload, settings)
                except Exception as exc:  # noqa: BLE001 - degrade, never lose the row
                    logger.warning(
                        "Failed to publish notification for alert rule %s: %s",
                        rule.id,
                        exc,
                    )
                fired += 1
        except Exception as exc:  # noqa: BLE001 - per-rule isolation
            await session.rollback()
            logger.warning(
                "Alert rule %s (%s) evaluation failed: %s",
                rule.id,
                rule.rule_type,
                exc,
            )
            continue

    return fired


def _evaluate_scheduled(
    rule: AlertRule, ticker: str, now: datetime
) -> str | None:
    """Evaluate a SCHEDULED rule (D-01, D-05).

    Fires a check-in reminder once its cadence interval has elapsed since
    the reference instant (``state["last_triggered_at"]`` if present,
    otherwise ``rule.created_at``) — never launches a research run.
    """
    cadence = rule.config["cadence"]
    interval = CADENCE_INTERVALS[cadence]

    state = rule.state or {}
    last_triggered_raw = state.get("last_triggered_at")
    if last_triggered_raw is not None:
        reference = datetime.fromisoformat(last_triggered_raw)
    else:
        reference = rule.created_at
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    if now - reference >= interval:
        rule.state = {
            **(rule.state or {}),
            "last_triggered_at": now.isoformat(),
            "last_checked_at": now.isoformat(),
        }
        return f"{cadence.capitalize()} check-in due for {ticker}"

    rule.state = {**(rule.state or {}), "last_checked_at": now.isoformat()}
    return None


async def _evaluate_new_filing(
    rule: AlertRule, ticker: str, now: datetime
) -> str | None:
    """Evaluate a NEW_FILING rule (D-02): stub — implemented in Task 2."""
    raise NotImplementedError


async def _evaluate_price_move(
    rule: AlertRule, ticker: str, now: datetime
) -> str | None:
    """Evaluate a PRICE_MOVE rule (D-05, Phase 10 D-10): stub — implemented in Task 2."""
    raise NotImplementedError


async def _newest_filing_hit(ticker: str) -> dict | None:
    """Return the newest EFTS hit for *ticker*, or ``None``: stub — implemented in Task 2."""
    raise NotImplementedError


__all__ = [
    "CADENCE_INTERVALS",
    "EFTS_LOOKBACK_DAYS",
    "evaluate_all_rules",
]
