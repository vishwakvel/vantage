"""Unit tests for app.services.progress_publisher — RED phase.

All Redis calls are mocked via an AsyncMock patched onto the module's
private redis factory. No live Redis instance is required.
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


def test_progress_channel_returns_deterministic_string() -> None:
    from app.services.progress_publisher import progress_channel

    assert progress_channel("abc") == "research:progress:abc"
    assert progress_channel("m1") == "research:progress:m1"


class TestPublishAgentStatus:
    @pytest.mark.anyio
    async def test_publishes_agent_event_to_memo_channel(self) -> None:
        from app.services.progress_publisher import (
            progress_channel,
            publish_agent_status,
        )

        mock_redis = AsyncMock()
        settings = _make_settings()

        with patch(
            "app.services.progress_publisher._redis", return_value=mock_redis
        ):
            await publish_agent_status(
                memo_id="m1",
                agent_type="FundamentalAnalysis",
                status="RUNNING",
                settings=settings,
            )

        mock_redis.publish.assert_awaited_once()
        channel, message = mock_redis.publish.await_args.args
        assert channel == progress_channel("m1")
        assert json.loads(message) == {
            "type": "agent",
            "agent_type": "FundamentalAnalysis",
            "status": "RUNNING",
        }

    @pytest.mark.anyio
    async def test_zero_subscribers_is_not_an_error(self) -> None:
        """redis.publish returning 0 (no subscribers) must not raise."""
        from app.services.progress_publisher import publish_agent_status

        mock_redis = AsyncMock()
        mock_redis.publish.return_value = 0
        settings = _make_settings()

        with patch(
            "app.services.progress_publisher._redis", return_value=mock_redis
        ):
            await publish_agent_status(
                memo_id="m1",
                agent_type="SentimentNLP",
                status="SUCCESS",
                settings=settings,
            )

        mock_redis.publish.assert_awaited_once()


class TestPublishMemoTerminal:
    @pytest.mark.anyio
    async def test_publishes_memo_event_to_same_channel(self) -> None:
        from app.services.progress_publisher import (
            progress_channel,
            publish_memo_terminal,
        )

        mock_redis = AsyncMock()
        settings = _make_settings()

        with patch(
            "app.services.progress_publisher._redis", return_value=mock_redis
        ):
            await publish_memo_terminal(
                memo_id="m1", memo_status="PARTIAL", settings=settings
            )

        mock_redis.publish.assert_awaited_once()
        channel, message = mock_redis.publish.await_args.args
        assert channel == progress_channel("m1")
        assert json.loads(message) == {"type": "memo", "status": "PARTIAL"}
