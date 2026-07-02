"""Shared async token-bucket rate limiter and real Groq client for all Groq
API calls.

Capacity: ~6,000 tokens/min.  All Groq callers MUST use this module.
Direct groq imports in app/agents/ or app/graph/ are prohibited and detected
by the import guard test (plan 01-08).
"""

import asyncio
import time

from groq import AsyncGroq

from app.core.config import get_settings

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
# Module-level Groq SDK client — lazily created, never cached at import time
# ---------------------------------------------------------------------------

_client: AsyncGroq | None = None


def _get_client(api_key: str) -> AsyncGroq:
    """Return the module-level AsyncGroq client, creating it on first use.

    The client is created lazily (never at module import time) because the
    API key comes from Settings, and Settings validates ALL required fields
    (DATABASE_URL, JWT_SECRET_KEY, GROQ_API_KEY) eagerly on instantiation —
    reading it at import time would break any test/script that imports this
    module without a full .env configured (same caveat already documented
    for EDGAR_USER_AGENT in app/core/config.py).
    """
    global _client
    if _client is None:
        _client = AsyncGroq(api_key=api_key)
    return _client


async def call_groq(
    prompt: str,
    model: str = "llama-3.3-70b-versatile",
    max_tokens: int = 1024,
) -> str:
    """Perform a real, rate-limited Groq chat-completion call.

    Acquires *max_tokens* from the shared rate limiter before making any
    request to the Groq API, then sends a single-turn chat completion and
    returns the response text. Groq SDK errors (APIConnectionError,
    RateLimitError, APIStatusError) are not caught here — they propagate to
    the caller; the SDK's own retry/backoff plus the rate limiter above
    already cover transient failures, so no hand-rolled retry loop is added.

    Args:
        prompt:     The prompt text to send to Groq.
        model:      Groq model identifier (default: llama-3.3-70b-versatile).
        max_tokens: Token budget to reserve from the rate limiter and pass
                    to the Groq API as the completion's max_tokens.

    Returns:
        The completion text (response.choices[0].message.content).
    """
    await groq_rate_limiter.acquire(max_tokens)
    client = _get_client(get_settings().GROQ_API_KEY)
    response = await client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content
