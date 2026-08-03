"""Celery application factory for Vantage's background research task (EXEC-05).

NOTE: broker and result backend are both sourced from ``Settings.REDIS_URL``
rather than a dedicated ``CELERY_BROKER_URL`` / ``CELERY_RESULT_BACKEND``
setting. Redis is already a required, single-source-of-truth dependency in
this project (JWT revocation via ``app.core.dependencies.get_redis``), and
Celery needs a broker/backend anyway — introducing a second Redis connection
string would duplicate config for no operational benefit and risk the two
URLs silently drifting apart across environments. If a dedicated Celery
broker ever becomes necessary (e.g. a separate Redis instance for queueing),
add a new explicit Settings field then — do not hardcode a fallback URL here.

Beat schedule (WATCH-09, D-04): ``app.workers.alert_tasks`` registers exactly
ONE periodic task, ``evaluate_alert_rules``, covering all three alert rule
types (NEW_FILING, PRICE_MOVE, SCHEDULED) in a single pass — never three
per-type beat entries, because each type's own due-ness logic already lives
inside the evaluator. ``celery_app.conf.timezone`` is pinned to ``"UTC"``
because the evaluator computes every persisted state timestamp with
``datetime.now(timezone.utc)``, so beat and the persisted state must share
one clock regardless of host locale.

A second entry, ``evaluate-retrieval-quality`` (OBS-03), runs
``app.workers.eval_tasks``'s offline RAGAS golden-set evaluation on a daily
cadence — a scheduled batch job with no latency requirement that makes zero
Groq calls, so it cannot compete with interactive research runs for the
shared rate-limited budget (D-09).
"""

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "vantage",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.tasks", "app.workers.alert_tasks", "app.workers.eval_tasks"],
)

# Makes the STARTED state observable (task has been picked up by a worker,
# not just queued) — needed for accurate per-agent progress reporting.
celery_app.conf.task_track_started = True

#: D-04's suggested default cadence: 15 minutes. Cheap for PRICE_MOVE and
#: NEW_FILING checks (one bounded external call per enabled rule) and
#: harmless granularity for SCHEDULED's daily/weekly/monthly cadences. A
#: named constant (rather than an inlined 900.0) keeps the interval tunable
#: from one place and assertable from a test.
ALERT_EVALUATION_INTERVAL_SECONDS: float = 900.0

#: OBS-03's suggested default cadence: daily (86400 seconds). This is a
#: scheduled batch job with no latency requirement that makes zero Groq
#: calls, so its cost is bounded retrieval work — daily is a reasonable
#: default; tune later per the Phase 11 "suggest an interval, tune later"
#: precedent. A named constant (rather than an inlined 86400.0) keeps the
#: interval tunable from one place and assertable from a test.
RAGAS_EVAL_INTERVAL_SECONDS: float = 86400.0

# Deliberately ONE beat entry covering all three alert rule types (D-04),
# not three per-type entries — per-type due-ness already lives inside
# app.services.alert_evaluation_service.evaluate_all_rules.
celery_app.conf.beat_schedule = {
    "evaluate-alert-rules": {
        "task": "evaluate_alert_rules",
        "schedule": ALERT_EVALUATION_INTERVAL_SECONDS,
    },
    "evaluate-retrieval-quality": {
        "task": "evaluate_retrieval_quality",
        "schedule": RAGAS_EVAL_INTERVAL_SECONDS,
    },
}

# The evaluator computes every state timestamp with
# datetime.now(timezone.utc) — pinning beat to UTC keeps the schedule and
# the persisted state on one clock regardless of host locale.
celery_app.conf.timezone = "UTC"
