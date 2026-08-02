"""Unit tests for app.services.api_call_counter — RED phase.

All Redis calls are mocked via an AsyncMock patched onto the module's
private redis factory (``_redis``), following
``tests/services/test_progress_publisher.py``'s style. No live Redis
instance is required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_settings():
    """Return a minimal Settings-like object without requiring a real .env."""
    s = MagicMock()
    s.REDIS_URL = "redis://localhost:6379/0"
    return s


@pytest.fixture(autouse=True)
def _reset_current_plan_id():
    """Reset the ambient ContextVar between tests so state cannot leak."""
    from app.services.api_call_counter import set_current_plan_id

    set_current_plan_id(None)
    yield
    set_current_plan_id(None)


def test_api_call_counter_key_returns_deterministic_string() -> None:
    from app.services.api_call_counter import api_call_counter_key

    assert api_call_counter_key("abc") == "research:api-calls:abc"


class TestIncrementApiCallCount:
    @pytest.mark.anyio
    async def test_increments_with_explicit_plan_id(self) -> None:
        from app.services.api_call_counter import (
            api_call_counter_key,
            increment_api_call_count,
        )

        mock_redis = AsyncMock()
        settings = _make_settings()

        with patch(
            "app.services.api_call_counter._redis", return_value=mock_redis
        ):
            await increment_api_call_count(plan_id="p1", settings=settings)

        mock_redis.incr.assert_awaited_once_with(api_call_counter_key("p1"))
        mock_redis.expire.assert_awaited_once()
        expire_args = mock_redis.expire.await_args.args
        assert expire_args[0] == api_call_counter_key("p1")

    @pytest.mark.anyio
    async def test_no_plan_id_anywhere_is_a_silent_no_op(self) -> None:
        from app.services.api_call_counter import increment_api_call_count

        settings = _make_settings()

        with patch("app.services.api_call_counter._redis") as mock_factory:
            await increment_api_call_count(settings=settings)

        mock_factory.assert_not_called()

    @pytest.mark.anyio
    async def test_uses_ambient_scope_plan_id_when_no_explicit_arg(self) -> None:
        from app.services.api_call_counter import (
            api_call_counter_key,
            increment_api_call_count,
            set_current_plan_id,
        )

        mock_redis = AsyncMock()
        settings = _make_settings()
        set_current_plan_id("ambient-plan")

        with patch(
            "app.services.api_call_counter._redis", return_value=mock_redis
        ):
            await increment_api_call_count(settings=settings)

        mock_redis.incr.assert_awaited_once_with(
            api_call_counter_key("ambient-plan")
        )

    @pytest.mark.anyio
    async def test_redis_incr_failure_does_not_propagate(self) -> None:
        from app.services.api_call_counter import increment_api_call_count

        mock_redis = AsyncMock()
        mock_redis.incr.side_effect = ConnectionError("redis down")
        settings = _make_settings()

        with patch(
            "app.services.api_call_counter._redis", return_value=mock_redis
        ):
            result = await increment_api_call_count(plan_id="p1", settings=settings)

        assert result is None


class TestReadAndClearApiCallCount:
    @pytest.mark.anyio
    async def test_returns_int_and_clears_key(self) -> None:
        from app.services.api_call_counter import (
            api_call_counter_key,
            read_and_clear_api_call_count,
        )

        mock_redis = AsyncMock()
        mock_redis.get.return_value = "7"
        settings = _make_settings()

        with patch(
            "app.services.api_call_counter._redis", return_value=mock_redis
        ):
            result = await read_and_clear_api_call_count("p1", settings=settings)

        assert result == 7
        mock_redis.delete.assert_awaited_once_with(api_call_counter_key("p1"))

    @pytest.mark.anyio
    async def test_returns_zero_when_key_absent(self) -> None:
        from app.services.api_call_counter import read_and_clear_api_call_count

        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        settings = _make_settings()

        with patch(
            "app.services.api_call_counter._redis", return_value=mock_redis
        ):
            result = await read_and_clear_api_call_count("p1", settings=settings)

        assert result == 0

    @pytest.mark.anyio
    async def test_returns_zero_when_redis_raises(self) -> None:
        from app.services.api_call_counter import read_and_clear_api_call_count

        settings = _make_settings()

        with patch(
            "app.services.api_call_counter._redis",
            side_effect=ConnectionError("redis down"),
        ):
            result = await read_and_clear_api_call_count("p1", settings=settings)

        assert result == 0


def test_two_plan_ids_derive_two_different_keys() -> None:
    from app.services.api_call_counter import api_call_counter_key

    assert api_call_counter_key("plan-a") != api_call_counter_key("plan-b")


class TestCrossRunIsolationAndUnscopedNoOp:
    """Pins the two properties D-05 exists to guarantee (RESEARCH.md Security
    Domain: Redis counter key collision/leak across concurrent research
    runs) — a naive global counter would violate both.
    """

    @pytest.mark.anyio
    async def test_T_12_03_LEAK_two_concurrent_runs_increment_distinct_keys(
        self,
    ) -> None:
        """Two distinct plan ids driven against one shared mock Redis must
        target two distinct, non-constant keys — never a shared/global key.
        """
        from app.services.api_call_counter import (
            api_call_counter_key,
            increment_api_call_count,
        )

        shared_mock_redis = AsyncMock()
        settings = _make_settings()

        with patch(
            "app.services.api_call_counter._redis",
            return_value=shared_mock_redis,
        ):
            await increment_api_call_count(plan_id="run-alpha", settings=settings)
            await increment_api_call_count(plan_id="run-beta", settings=settings)

        incr_keys = [call.args[0] for call in shared_mock_redis.incr.await_args_list]
        assert incr_keys == [
            api_call_counter_key("run-alpha"),
            api_call_counter_key("run-beta"),
        ]
        # Neither key is a shared/constant string — each is plan-id-derived.
        assert len(set(incr_keys)) == 2
        assert "research:api-calls:" not in incr_keys  # no bare/constant key

    @pytest.mark.anyio
    async def test_T_12_03_STRAY_unscoped_call_never_constructs_redis_client(
        self,
    ) -> None:
        """When no plan id is resolvable, the module's ``_redis`` factory
        itself must receive zero calls — not merely that ``incr`` went
        unawaited. Once alert-evaluation-triggered EDGAR calls are
        instrumented, an unscoped increment that still opened a Redis
        connection would leak one connection per alert tick forever.
        """
        from app.services.api_call_counter import (
            increment_api_call_count,
            set_current_plan_id,
        )

        set_current_plan_id(None)
        settings = _make_settings()

        with patch("app.services.api_call_counter._redis") as mock_redis_factory:
            await increment_api_call_count(settings=settings)

        mock_redis_factory.assert_not_called()
