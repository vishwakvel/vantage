"""Shared async token-bucket rate limiter for all Groq API calls.

Capacity: ~6,000 tokens/min.  All Groq callers MUST use this module.
Direct groq imports in app/agents/ or app/graph/ are prohibited and detected
by the import guard test (plan 01-08).
"""

import asyncio
import time

# ---------------------------------------------------------------------------
# Rate-limiter constants
# ---------------------------------------------------------------------------

_BUCKET_CAPACITY: float = 6000.0  # ~6,000 tokens per minute
_REFILL_RATE: float = 100.0  # tokens per second (6000 / 60)


class AsyncTokenBucketRateLimiter:
    """Async token-bucket rate limiter.

    Callers block (await) when the bucket is empty — requests are never
    dropped and no exception is raised due to exhaustion.

    Args:
        capacity:    Maximum token capacity (default: 6000 tokens/min).
        refill_rate: Tokens added per second (default: 100 tokens/s).
    """

    def __init__(
        self,
        capacity: float = _BUCKET_CAPACITY,
        refill_rate: float = _REFILL_RATE,
    ) -> None:
        self.capacity: float = capacity
        self.refill_rate: float = refill_rate  # tokens/second
        self._tokens: float = capacity
        self._last_refill: float = time.monotonic()
        self._lock: asyncio.Lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> None:
        """Acquire *tokens* from the bucket, blocking until available.

        This method never raises due to token exhaustion; it awaits until the
        bucket has enough tokens, then deducts them and returns.

        Args:
            tokens: Number of tokens to consume (default: 1).
        """
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self.capacity,
                    self._tokens + elapsed * self.refill_rate,
                )
                self._last_refill = now

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return

                # Not enough tokens — compute wait time and yield control
                wait_seconds = (tokens - self._tokens) / self.refill_rate
                await asyncio.sleep(wait_seconds)


# ---------------------------------------------------------------------------
# Module-level singleton — import this; do NOT instantiate a second limiter
# ---------------------------------------------------------------------------

groq_rate_limiter: AsyncTokenBucketRateLimiter = AsyncTokenBucketRateLimiter()


# ---------------------------------------------------------------------------
# call_groq stub — Phase 1: token tracking only, no real LLM calls
# ---------------------------------------------------------------------------


async def call_groq(
    prompt: str,
    model: str = "mixtral-8x7b-32768",
    max_tokens: int = 1024,
) -> str:
    """Stub: acquires rate-limit budget then raises NotImplementedError.

    Real Groq API integration is implemented in Phase 4.  Raising here
    ensures no accidental LLM calls leak through in Phase 1 tests or CI.

    Args:
        prompt:     The prompt text to send to Groq (unused in Phase 1).
        model:      Groq model identifier (unused in Phase 1).
        max_tokens: Token budget to reserve from the rate limiter.

    Raises:
        NotImplementedError: Always — Groq API calls not implemented in Phase 1.
    """
    await groq_rate_limiter.acquire(max_tokens)
    raise NotImplementedError("Groq API calls not implemented in Phase 1")
