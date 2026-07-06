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
"""

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "vantage",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.tasks"],
)

# Makes the STARTED state observable (task has been picked up by a worker,
# not just queued) — needed for accurate per-agent progress reporting.
celery_app.conf.task_track_started = True
