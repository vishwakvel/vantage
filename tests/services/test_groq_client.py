"""Unit tests for Groq async token-bucket rate limiter.

Tests verify:
  - AsyncTokenBucketRateLimiter.acquire() completes immediately on a full bucket
  - acquire() blocks (awaits) when tokens are exhausted — never raises, never drops
  - groq_rate_limiter module-level singleton exists with correct capacity
  - call_groq() raises NotImplementedError (Phase 1 stub)
"""

import asyncio
import time

import pytest

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
# call_groq stub raises NotImplementedError
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_call_groq_raises_not_implemented() -> None:
    """call_groq() is a Phase 1 stub — always raises NotImplementedError."""
    with pytest.raises(NotImplementedError) as exc_info:
        await call_groq("test prompt")
    assert "Phase 1" in str(exc_info.value)


@pytest.mark.anyio
async def test_call_groq_raises_for_any_prompt() -> None:
    """call_groq() raises regardless of prompt content."""
    with pytest.raises(NotImplementedError):
        await call_groq("", model="mixtral-8x7b-32768", max_tokens=512)
