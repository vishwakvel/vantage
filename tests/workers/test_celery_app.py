"""Tests for app.workers.celery_app (EXEC-05).

These tests only inspect the module-level ``celery_app`` configuration —
they never call ``.delay()`` or otherwise require a running broker.
"""

from app.core.config import get_settings
from app.workers.celery_app import celery_app


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
