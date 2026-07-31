"""Unit tests for app.services.live_price_source (11-02-PLAN.md).

Patches ``LivePriceSource._fetch_fast_info`` (a staticmethod, patched on the
class) rather than ``yfinance`` itself — this keeps the tests hermetic while
still exercising the real ``asyncio.to_thread`` dispatch path. No live
network call is made in any test here.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from app.services.live_price_source import LivePriceSource

pytestmark = pytest.mark.anyio


async def test_happy_path_returns_float_price() -> None:
    source = LivePriceSource()

    with patch.object(
        LivePriceSource,
        "_fetch_fast_info",
        return_value={"last_price": 187.42},
    ):
        price = await source.get_current_price("AAPL")

    assert price == 187.42
    assert isinstance(price, float)


async def test_fetch_raising_returns_none() -> None:
    source = LivePriceSource()

    with patch.object(
        LivePriceSource,
        "_fetch_fast_info",
        side_effect=RuntimeError("boom"),
    ):
        price = await source.get_current_price("AAPL")

    assert price is None


async def test_missing_last_price_key_returns_none() -> None:
    source = LivePriceSource()

    with patch.object(LivePriceSource, "_fetch_fast_info", return_value={}):
        price = await source.get_current_price("AAPL")

    assert price is None


async def test_last_price_none_returns_none() -> None:
    source = LivePriceSource()

    with patch.object(
        LivePriceSource,
        "_fetch_fast_info",
        return_value={"last_price": None},
    ):
        price = await source.get_current_price("AAPL")

    assert price is None


@pytest.mark.parametrize(
    "unusable_value",
    [float("nan"), float("inf"), 0.0, -5.0],
)
async def test_unusable_values_return_none(unusable_value: float) -> None:
    source = LivePriceSource()

    with patch.object(
        LivePriceSource,
        "_fetch_fast_info",
        return_value={"last_price": unusable_value},
    ):
        price = await source.get_current_price("AAPL")

    assert price is None


async def test_string_last_price_is_coerced_to_float() -> None:
    source = LivePriceSource()

    with patch.object(
        LivePriceSource,
        "_fetch_fast_info",
        return_value={"last_price": "187.42"},
    ):
        price = await source.get_current_price("AAPL")

    assert price == 187.42
    assert isinstance(price, float)


def test_get_current_price_dispatches_through_a_worker_thread() -> None:
    """Source-level assertion that the coroutine offloads via asyncio.to_thread."""
    assert "to_thread" in inspect.getsource(LivePriceSource.get_current_price)
