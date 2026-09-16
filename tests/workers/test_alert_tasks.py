"""Tests for app.workers.alert_tasks (11-05-PLAN.md, WATCH-09/D-04).

No broker, no DB, no Redis is used anywhere in this module — every
collaborator is patched. This mirrors tests/workers/test_celery_app.py's
own docstring rule: inspect configuration/wiring only, never enqueue a real
task via Celery's async-dispatch API, never require a running broker.
"""

from __future__ import annotations

import inspect
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers.alert_tasks import _evaluate_all_rules_async, evaluate_alert_rules_task
from app.workers.celery_app import celery_app


def test_task_registered_under_expected_name() -> None:
    """A name mismatch here produces a beat entry that enqueues a task no
    worker can route — silently, with no error anywhere."""
    assert evaluate_alert_rules_task.name == "evaluate_alert_rules"
    assert (
        celery_app.conf.beat_schedule["evaluate-alert-rules"]["task"]
        == evaluate_alert_rules_task.name
    )


def test_task_resets_singletons_before_asyncio_run() -> None:
    parent = MagicMock()

    with (
        patch("app.workers.alert_tasks.reset_session_factory", parent.reset_session_factory),
        patch("app.workers.alert_tasks.reset_edgar_client", parent.reset_edgar_client),
        patch("app.workers.alert_tasks.asyncio.run", parent.asyncio_run),
    ):
        evaluate_alert_rules_task()

    parent.reset_session_factory.assert_called_once()
    parent.reset_edgar_client.assert_called_once()
    parent.asyncio_run.assert_called_once()

    call_names = [c[0] for c in parent.mock_calls]
    assert call_names.index("reset_session_factory") < call_names.index("asyncio_run")
    assert call_names.index("reset_edgar_client") < call_names.index("asyncio_run")

    # asyncio.run is mocked (never actually awaited), so the real coroutine
    # object it was called with is never driven to completion — close it
    # explicitly to avoid a "coroutine was never awaited" RuntimeWarning
    # leaking into unrelated tests at garbage-collection time.
    parent.asyncio_run.call_args.args[0].close()


def test_task_takes_no_arguments() -> None:
    assert inspect.signature(evaluate_alert_rules_task.run).parameters == {}


@pytest.mark.anyio
async def test_async_body_delegates_to_evaluator() -> None:
    fake_session = MagicMock()

    @asynccontextmanager
    async def _fake_session_scope():
        yield fake_session

    mock_evaluate = AsyncMock(return_value=2)

    with (
        patch("app.workers.alert_tasks.session_scope", _fake_session_scope),
        patch("app.workers.alert_tasks.evaluate_all_rules", mock_evaluate),
    ):
        await _evaluate_all_rules_async()

    mock_evaluate.assert_awaited_once_with(fake_session)


@pytest.mark.anyio
async def test_async_body_swallows_and_logs_infrastructure_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_session = MagicMock()

    @asynccontextmanager
    async def _fake_session_scope():
        yield fake_session

    mock_evaluate = AsyncMock(side_effect=RuntimeError("db unreachable"))

    # Guard against cross-test logging pollution: tests/db/test_migrations.py
    # runs alembic's env.py, which calls logging.config.fileConfig() with its
    # default disable_existing_loggers=True — that silently disables any
    # logger created before it (including this module's, imported at
    # collection time), so caplog would otherwise miss the record purely
    # based on test execution order. Re-enabling here makes this test
    # order-independent.
    logging.getLogger("app.workers.alert_tasks").disabled = False

    with (
        patch("app.workers.alert_tasks.session_scope", _fake_session_scope),
        patch("app.workers.alert_tasks.evaluate_all_rules", mock_evaluate),
        caplog.at_level(logging.ERROR, logger="app.workers.alert_tasks"),
    ):
        await _evaluate_all_rules_async()  # must not raise

    assert any(record.levelno >= logging.ERROR for record in caplog.records)
