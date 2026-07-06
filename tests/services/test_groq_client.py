"""Unit tests for the Groq async token-bucket rate limiter and call_groq.

Tests verify:
  - AsyncTokenBucketRateLimiter.acquire() completes immediately on a full bucket
  - acquire() blocks (awaits) when tokens are exhausted — never raises, never drops
  - groq_rate_limiter module-level singleton exists with correct capacity
  - call_groq() performs a real (SDK-boundary-mocked) Groq chat-completion:
    it acquires the rate limiter before calling the SDK, returns the
    completion text, defaults to llama-3.3-70b-versatile, and invokes the
    SDK with the expected messages/model/max_tokens.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.groq_client as groq_client_module
from app.services.groq_client import (
    AsyncTokenBucketRateLimiter,
    call_groq,
    groq_rate_limiter,
)

# ---------------------------------------------------------------------------
# Singleton and capacity
# ---------------------------------------------------------------------------


def test_groq_rate_limiter_singleton_exists() -> None:
    """groq_rate_limiter is importable as a module-level singleton."""
    assert groq_rate_limiter is not None
    assert isinstance(groq_rate_limiter, AsyncTokenBucketRateLimiter)


def test_groq_rate_limiter_default_capacity() -> None:
    """Default capacity is 6000.0 tokens."""
    assert groq_rate_limiter.capacity == 6000.0


def test_groq_rate_limiter_default_refill_rate() -> None:
    """Default refill rate is 100.0 tokens/second (6000 / 60)."""
    assert groq_rate_limiter.refill_rate == 100.0


# ---------------------------------------------------------------------------
# Immediate acquisition on a full bucket
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_acquire_single_token_full_bucket_is_immediate() -> None:
    """acquire(1) on a full bucket does not sleep and returns promptly."""
    limiter = AsyncTokenBucketRateLimiter(capacity=10, refill_rate=10.0)
    start = time.monotonic()
    await limiter.acquire(1)
    elapsed = time.monotonic() - start
    # Should complete well under 0.1 seconds (no sleep needed)
    assert elapsed < 0.1


@pytest.mark.anyio
async def test_acquire_full_capacity_is_immediate() -> None:
    """acquire(capacity) on a full bucket returns without sleeping."""
    limiter = AsyncTokenBucketRateLimiter(capacity=10, refill_rate=10.0)
    start = time.monotonic()
    await limiter.acquire(10)
    elapsed = time.monotonic() - start
    assert elapsed < 0.1


# ---------------------------------------------------------------------------
# Blocking (awaiting) at 0 tokens — never drops
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_acquire_blocks_when_tokens_exhausted() -> None:
    """acquire() awaits (does not raise) when the bucket is exhausted.

    Drains the bucket completely, then issues one more acquire().
    The second acquire must complete after tokens refill — it must NOT raise.
    """
    refill_rate = 100.0  # tokens/second
    capacity = 10
    limiter = AsyncTokenBucketRateLimiter(capacity=capacity, refill_rate=refill_rate)

    # Drain the bucket
    await limiter.acquire(capacity)

    # Now the bucket is empty.  acquire(1) should block ~1/100 = 0.01 s
    start = time.monotonic()
    await limiter.acquire(1)
    elapsed = time.monotonic() - start

    # Must have waited (blocked), not raised
    assert elapsed >= 0.005  # at least 5 ms — proves it waited


@pytest.mark.anyio
async def test_acquire_never_raises_on_empty_bucket() -> None:
    """acquire() on an empty bucket awaits; no exception is ever raised."""
    limiter = AsyncTokenBucketRateLimiter(capacity=5, refill_rate=50.0)
    await limiter.acquire(5)  # drain

    # Must not raise — must return (after blocking)
    try:
        await asyncio.wait_for(limiter.acquire(1), timeout=2.0)
    except TimeoutError:
        pytest.fail("acquire() timed out — should have unblocked after refill")
    except Exception as exc:
        pytest.fail(f"acquire() raised {type(exc).__name__}: {exc}")


@pytest.mark.anyio
async def test_acquire_concurrent_callers_all_complete() -> None:
    """Multiple concurrent callers all complete without exception."""
    limiter = AsyncTokenBucketRateLimiter(capacity=3, refill_rate=100.0)

    results: list[str] = []

    async def caller(name: str) -> None:
        await limiter.acquire(2)
        results.append(name)

    # Run 3 coroutines concurrently — each needs 2 tokens (6 total) with capacity 3
    await asyncio.gather(
        asyncio.wait_for(caller("a"), timeout=5.0),
        asyncio.wait_for(caller("b"), timeout=5.0),
        asyncio.wait_for(caller("c"), timeout=5.0),
    )
    assert sorted(results) == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# call_groq — real Groq chat-completion (SDK boundary mocked)
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Minimal Settings stand-in exposing only what call_groq reads."""

    GROQ_API_KEY = "test-groq-key-not-for-production"


def _make_mock_client(content: str = "mocked completion text") -> MagicMock:
    """Build a mock AsyncGroq client whose chat.completions.create returns *content*."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content=content))]
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    return mock_client


@pytest.fixture(autouse=True)
def _reset_client_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure every test starts with a clean module-level client singleton."""
    monkeypatch.setattr(groq_client_module, "_client", None, raising=False)


def test_call_groq_default_model_is_llama_3_3_70b_versatile() -> None:
    """call_groq's default model parameter is llama-3.3-70b-versatile."""
    import inspect

    sig = inspect.signature(call_groq)
    assert sig.parameters["model"].default == "llama-3.3-70b-versatile"


@pytest.mark.anyio
async def test_call_groq_acquires_rate_limit_before_sdk_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """call_groq awaits groq_rate_limiter.acquire(max_tokens) before invoking the SDK."""
    call_order: list[str] = []
    original_acquire = groq_rate_limiter.acquire

    async def _tracking_acquire(tokens: int) -> None:
        call_order.append("acquire")
        await original_acquire(tokens)

    monkeypatch.setattr(groq_rate_limiter, "acquire", _tracking_acquire)

    mock_client = _make_mock_client()

    async def _tracking_create(*args: object, **kwargs: object) -> object:
        call_order.append("sdk_call")
        return mock_client.chat.completions.create.return_value

    mock_client.chat.completions.create = AsyncMock(side_effect=_tracking_create)
    monkeypatch.setattr(groq_client_module, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(groq_client_module, "AsyncGroq", lambda api_key: mock_client)

    await call_groq("test prompt", max_tokens=10)

    assert call_order == ["acquire", "sdk_call"]


@pytest.mark.anyio
async def test_call_groq_returns_completion_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """call_groq returns response.choices[0].message.content."""
    mock_client = _make_mock_client(content="hello from groq")
    monkeypatch.setattr(groq_client_module, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(groq_client_module, "AsyncGroq", lambda api_key: mock_client)

    result = await call_groq("test prompt", max_tokens=10)

    assert result == "hello from groq"


@pytest.mark.anyio
async def test_call_groq_invokes_sdk_with_expected_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AsyncGroq client is invoked with messages/model/max_tokens as specified."""
    mock_client = _make_mock_client()
    monkeypatch.setattr(groq_client_module, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(groq_client_module, "AsyncGroq", lambda api_key: mock_client)

    await call_groq("what is the ticker?", model="llama-3.3-70b-versatile", max_tokens=256)

    mock_client.chat.completions.create.assert_awaited_once_with(
        messages=[{"role": "user", "content": "what is the ticker?"}],
        model="llama-3.3-70b-versatile",
        max_tokens=256,
    )


# ---------------------------------------------------------------------------
# reset_groq_client — event-loop safety across Celery task boundaries
# ---------------------------------------------------------------------------


def test_reset_groq_client_drops_the_cached_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """reset_groq_client() drops the lazy module-level AsyncGroq singleton so
    the next call_groq() rebuilds it bound to the current event loop.

    Mirrors app/db/session.py::reset_session_factory exactly: each Celery
    task runs the async research graph under its own fresh asyncio.run(...)
    event loop, and an AsyncGroq client (which wraps an httpx.AsyncClient
    internally) created inside a prior task's now-closed loop cannot be
    safely reused inside a new one.
    """
    sentinel = object()
    monkeypatch.setattr(groq_client_module, "_client", sentinel, raising=False)
    assert groq_client_module._client is sentinel

    groq_client_module.reset_groq_client()

    assert groq_client_module._client is None
