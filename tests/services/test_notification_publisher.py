"""Unit tests for app.services.notification_publisher (11-02-PLAN.md).

All Redis calls are mocked via an AsyncMock patched onto the module's
private redis factory, mirroring tests/services/test_progress_publisher.py.
No live Redis instance is required.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_settings():
    """Return a minimal Settings-like object without requiring a real .env."""
    s = MagicMock()
    s.REDIS_URL = "redis://localhost:6379/0"
    return s


def test_notification_channel_returns_deterministic_string() -> None:
    from app.services.notification_publisher import notification_channel

    assert notification_channel("u1") == "notifications:u1"
    assert notification_channel("u2") == "notifications:u2"


def test_notification_payload_key_set() -> None:
    from app.services.notification_publisher import notification_payload

    payload = notification_payload("e1", "r1", "AAPL dropped 6.2%", "2026-07-28T10:00:00+00:00")

    assert sorted(payload) == ["alert_rule_id", "id", "message", "read", "triggered_at"]
    assert payload["read"] is False


class TestPublishNotification:
    @pytest.mark.anyio
    async def test_publishes_on_user_channel(self) -> None:
        from app.services.notification_publisher import (
            notification_channel,
            notification_payload,
            publish_notification,
        )

        mock_redis = AsyncMock()
        settings = _make_settings()

        with patch("app.services.notification_publisher._redis", return_value=mock_redis):
            await publish_notification(
                "u1",
                notification_payload("e1", "r1", "AAPL dropped 6.2%", "2026-07-28T10:00:00+00:00"),
                settings=settings,
            )

        mock_redis.publish.assert_awaited_once()
        channel, message = mock_redis.publish.await_args.args
        assert channel == notification_channel("u1")
        assert json.loads(message) == {
            "type": "notification",
            "id": "e1",
            "alert_rule_id": "r1",
            "message": "AAPL dropped 6.2%",
            "triggered_at": "2026-07-28T10:00:00+00:00",
            "read": False,
        }

    @pytest.mark.anyio
    async def test_zero_subscribers_is_not_an_error(self) -> None:
        from app.services.notification_publisher import (
            notification_payload,
            publish_notification,
        )

        mock_redis = AsyncMock()
        mock_redis.publish.return_value = 0
        settings = _make_settings()

        with patch("app.services.notification_publisher._redis", return_value=mock_redis):
            await publish_notification(
                "u1",
                notification_payload("e1", "r1", "m", "t"),
                settings=settings,
            )

        mock_redis.publish.assert_awaited_once()

    @pytest.mark.anyio
    async def test_redis_client_closed_on_success(self) -> None:
        from app.services.notification_publisher import (
            notification_payload,
            publish_notification,
        )

        mock_redis = AsyncMock()
        settings = _make_settings()

        with patch("app.services.notification_publisher._redis", return_value=mock_redis):
            await publish_notification(
                "u1",
                notification_payload("e1", "r1", "m", "t"),
                settings=settings,
            )

        mock_redis.close.assert_awaited_once()

    @pytest.mark.anyio
    async def test_redis_client_closed_when_publish_raises(self) -> None:
        from app.services.notification_publisher import (
            notification_payload,
            publish_notification,
        )

        mock_redis = AsyncMock()
        mock_redis.publish.side_effect = RuntimeError("boom")
        settings = _make_settings()

        with patch("app.services.notification_publisher._redis", return_value=mock_redis):
            with pytest.raises(RuntimeError):
                await publish_notification(
                    "u1",
                    notification_payload("e1", "r1", "m", "t"),
                    settings=settings,
                )

        mock_redis.close.assert_awaited_once()
