"""Tests for app.workers.celery_app (EXEC-05, extended for WATCH-09/D-04).

These tests only inspect the module-level ``celery_app`` configuration —
they never call ``.delay()`` or otherwise require a running broker. Coverage
now also includes the ``app.workers.alert_tasks`` include-list registration,
the single ``evaluate-alert-rules`` beat entry (D-04: one entry, not three
per-type entries), and the UTC timezone pin.
"""

from app.core.config import get_settings
from app.workers.celery_app import ALERT_EVALUATION_INTERVAL_SECONDS, celery_app


def test_broker_and_backend_reuse_redis_url():
    """Both broker_url and result_backend must equal Settings.REDIS_URL."""
    settings = get_settings()
    assert celery_app.conf.broker_url == settings.REDIS_URL
    assert celery_app.conf.result_backend == settings.REDIS_URL


def test_tasks_module_registered_for_autodiscovery():
    """app.workers.tasks must be listed in celery_app.conf.include.

    This is a registration hint (not an eager import) — app.workers.tasks
    does not need to exist yet for this module to import cleanly.
    """
    assert "app.workers.tasks" in celery_app.conf.include


def test_alert_tasks_module_registered_for_autodiscovery():
    """app.workers.alert_tasks must be ADDED to include, alongside the
    pre-existing app.workers.tasks entry — a regression guard against
    replacing rather than appending to the list."""
    assert "app.workers.alert_tasks" in celery_app.conf.include
    assert "app.workers.tasks" in celery_app.conf.include


def test_beat_schedule_has_single_alert_entry():
    """D-04: exactly ONE beat entry covers all three alert rule types —
    never three per-type entries. Asserted structurally so a future
    per-type entry cannot be added without updating this test (and its
    D-04 justification)."""
    assert list(celery_app.conf.beat_schedule.keys()) == ["evaluate-alert-rules"]
    assert (
        celery_app.conf.beat_schedule["evaluate-alert-rules"]["schedule"]
        == ALERT_EVALUATION_INTERVAL_SECONDS
    )


def test_beat_timezone_is_utc():
    assert celery_app.conf.timezone == "UTC"
