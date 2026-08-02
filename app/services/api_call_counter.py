"""Per-research-run external-API call counter (D-05, OBS-02/MEMO-06).

This module is the single source of truth for the per-research-run external-API
call counter. It is keyed by ``plan_id`` and NOT by ``memo_id`` because EDGAR
ingestion runs before the memo row exists (RESEARCH.md Pitfall 3): the counter
must be readable and writable before a ``ResearchMemo`` exists at all.

Storage is Redis (not in-memory graph state) because the count accumulates
across two separate processes — the ``POST /research`` API request (where
EDGAR ingestion happens synchronously) and the later Celery research task
(where the agent graph runs and the memo is assembled). An in-memory counter
on ``AgentGraphState`` or a bare contextvar structurally cannot see calls made
in the other process.

Every function here is fail-soft: instrumentation must never be able to fail
a real research run. These functions are called from inside every external
service client (``edgar_client``, ``news_client``, ``arxiv_client``,
``fred_client``, ``financial_metrics_source``, ``comparables_source``), so a
Redis outage must degrade to a no-op / ``0``, never propagate.

A module-level ``ContextVar`` provides an ambient plan-id scope purely so
individual service clients can increment without threading a ``plan_id``
parameter through every signature. The durable state always lives in Redis —
the ContextVar is scope only, never storage. Each entry point (``POST
/research`` in plan 12-06, the Celery research task in plan 12-13) sets it
once at the start of its own lifecycle. A ``ContextVar`` is safe here because
FastAPI gives each request its own context and Celery's prefork workers run
one task per process at a time.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

import redis.asyncio as aioredis

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Leak safety net: the read path (``read_and_clear_api_call_count``) deletes
# the key on the happy path, so this TTL only matters when a run dies before
# reaching memo assembly.
_COUNTER_TTL_SECONDS: int = 3600

_current_plan_id: ContextVar[str | None] = ContextVar(
    "_current_plan_id", default=None
)


def api_call_counter_key(plan_id: str) -> str:
    """Return the deterministic per-plan Redis key for the API call counter.

    This is the single source of truth for the key — both the increment side
    and the read side must derive the key via this function, never by
    re-formatting the string inline.
    """
    return f"research:api-calls:{plan_id}"


def _redis(settings: Settings) -> aioredis.Redis:
    """Return a plain (non-``Depends``) Redis client from ``settings.REDIS_URL``.

    Identical call shape to ``app.services.progress_publisher._redis``, but
    callable outside FastAPI dependency injection — service clients and any
    Celery task run outside a request context.
    """
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


def set_current_plan_id(plan_id: str | None) -> None:
    """Set the ambient plan-id scope for the current context.

    Called once per lifecycle by each entry point (``POST /research``, the
    Celery research task) so downstream service clients can increment the
    counter without a ``plan_id`` parameter on every call site.
    """
    _current_plan_id.set(plan_id)


def get_current_plan_id() -> str | None:
    """Return the plan id currently set in the ambient scope, if any."""
    return _current_plan_id.get()


async def increment_api_call_count(
    plan_id: str | None = None,
    settings: Settings | None = None,
) -> None:
    """Increment the external-API call counter for the resolved plan id.

    The plan id is resolved as: the explicit ``plan_id`` argument if given,
    else the ambient scope (``get_current_plan_id()``). If no plan id can be
    resolved, this is a true no-op — no Redis client is constructed and
    nothing is awaited. This matters because ``alert_evaluation_service``
    calls ``edgar_client`` outside any research run; an unscoped call must
    never open a stray Redis connection.

    Never raises: a Redis failure here must not fail the real external-API
    call it is instrumenting.
    """
    resolved_plan_id = plan_id if plan_id is not None else get_current_plan_id()
    if not resolved_plan_id:
        return

    if settings is None:
        settings = get_settings()

    try:
        redis = _redis(settings)
        key = api_call_counter_key(resolved_plan_id)
        await redis.incr(key)
        await redis.expire(key, _COUNTER_TTL_SECONDS)
    except Exception:
        logger.warning(
            "api_call_counter: failed to increment counter", exc_info=True
        )


async def read_and_clear_api_call_count(
    plan_id: str,
    settings: Settings | None = None,
) -> int:
    """Return the accumulated call count for ``plan_id`` and clear the key.

    Read-and-clear (not read-only) so a re-run of the same plan starts from
    zero rather than inheriting the previous run's count. Returns ``0`` when
    the key is absent or when Redis raises — this function never propagates.
    """
    if settings is None:
        settings = get_settings()

    try:
        redis = _redis(settings)
        key = api_call_counter_key(plan_id)
        value = await redis.get(key)
        await redis.delete(key)
        return int(value) if value is not None else 0
    except Exception:
        logger.exception("api_call_counter: failed to read counter")
        return 0


__all__ = [
    "api_call_counter_key",
    "set_current_plan_id",
    "get_current_plan_id",
    "increment_api_call_count",
    "read_and_clear_api_call_count",
]
