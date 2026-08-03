"""Tests for app.workers.eval_tasks (12-12-PLAN.md, OBS-03).

No broker, no DB, no Redis, no real RAGAS scoring is used anywhere in this
module — every collaborator is patched. This mirrors
tests/workers/test_alert_tasks.py's own docstring rule: inspect
configuration/wiring and swallow-and-log behavior only, never enqueue a real
task via Celery's async-dispatch API, never require a running broker.
"""

from __future__ import annotations

import inspect
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.eval.golden_set import GoldenSetError
from app.workers.celery_app import celery_app
from app.workers.eval_tasks import (
    _evaluate_retrieval_quality_async,
    evaluate_retrieval_quality_task,
)


def test_task_registered_under_expected_name() -> None:
    """A name mismatch here produces a beat entry that enqueues a task no
    worker can route — silently, with no error anywhere. The beat_schedule
    entry itself is added and asserted in test_celery_app.py (12-12 Task 2);
    this test only proves the task is importable and named correctly."""
    assert evaluate_retrieval_quality_task.name == "evaluate_retrieval_quality"
    assert "evaluate_retrieval_quality" in celery_app.tasks


def test_task_takes_no_arguments() -> None:
    assert inspect.signature(evaluate_retrieval_quality_task.run).parameters == {}


def test_task_resets_session_factory_before_asyncio_run() -> None:
    parent = MagicMock()

    with (
        patch(
            "app.workers.eval_tasks.reset_session_factory", parent.reset_session_factory
        ),
        patch("app.workers.eval_tasks.asyncio.run", parent.asyncio_run),
    ):
        evaluate_retrieval_quality_task()

    parent.reset_session_factory.assert_called_once()
    parent.asyncio_run.assert_called_once()

    call_names = [c[0] for c in parent.mock_calls]
    assert call_names.index("reset_session_factory") < call_names.index("asyncio_run")

    # asyncio.run is mocked (never actually awaited), so the real coroutine
    # object it was called with is never driven to completion — close it
    # explicitly to avoid a "coroutine was never awaited" RuntimeWarning
    # leaking into unrelated tests at garbage-collection time.
    parent.asyncio_run.call_args.args[0].close()


@pytest.mark.anyio
async def test_async_body_delegates_to_evaluate_golden_set() -> None:
    fake_session = MagicMock()

    @asynccontextmanager
    async def _fake_session_scope():
        yield fake_session

    mock_evaluate = AsyncMock(return_value=[MagicMock(), MagicMock()])

    with (
        patch("app.workers.eval_tasks.session_scope", _fake_session_scope),
        patch("app.workers.eval_tasks.evaluate_golden_set", mock_evaluate),
    ):
        await _evaluate_retrieval_quality_async()

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
    logging.getLogger("app.workers.eval_tasks").disabled = False

    with (
        patch("app.workers.eval_tasks.session_scope", _fake_session_scope),
        patch("app.workers.eval_tasks.evaluate_golden_set", mock_evaluate),
        caplog.at_level(logging.ERROR, logger="app.workers.eval_tasks"),
    ):
        await _evaluate_retrieval_quality_async()  # must not raise

    assert any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.anyio
async def test_async_body_swallows_and_logs_golden_set_error_as_fixture_problem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_session = MagicMock()

    @asynccontextmanager
    async def _fake_session_scope():
        yield fake_session

    mock_evaluate = AsyncMock(side_effect=GoldenSetError("golden set is not a JSON list"))

    logging.getLogger("app.workers.eval_tasks").disabled = False

    with (
        patch("app.workers.eval_tasks.session_scope", _fake_session_scope),
        patch("app.workers.eval_tasks.evaluate_golden_set", mock_evaluate),
        caplog.at_level(logging.ERROR, logger="app.workers.eval_tasks"),
    ):
        await _evaluate_retrieval_quality_async()  # must not raise

    error_records = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert error_records
    assert any("fixture" in record.getMessage().lower() for record in error_records)


@pytest.mark.anyio
async def test_async_body_logs_summary_with_case_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_session = MagicMock()

    @asynccontextmanager
    async def _fake_session_scope():
        yield fake_session

    mock_evaluate = AsyncMock(return_value=[MagicMock(), MagicMock(), MagicMock()])

    logging.getLogger("app.workers.eval_tasks").disabled = False

    with (
        patch("app.workers.eval_tasks.session_scope", _fake_session_scope),
        patch("app.workers.eval_tasks.evaluate_golden_set", mock_evaluate),
        caplog.at_level(logging.INFO, logger="app.workers.eval_tasks"),
    ):
        await _evaluate_retrieval_quality_async()

    info_records = [record for record in caplog.records if record.levelno == logging.INFO]
    assert any("3" in record.getMessage() for record in info_records)
