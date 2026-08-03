"""Shared async token-bucket rate limiter and real Groq client for all Groq
API calls.

Capacity: ~6,000 tokens/min.  All Groq callers MUST use this module.
Direct groq imports in app/agents/ or app/graph/ are prohibited and detected
by the import guard test (plan 01-08).

``call_groq`` returns a ``GroqResult`` (text plus prompt/completion token
counts) rather than a bare string, and is decorated with LangSmith's
``@traceable`` so every Groq call in the codebase emits an ``llm``-type
trace when tracing is enabled (see ``call_groq``'s docstring for the
data-exposure implication of turning tracing on).
"""

import asyncio
import time
from dataclasses import dataclass

from groq import AsyncGroq
from langsmith import traceable

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


def reset_groq_client() -> None:
    """Drop the lazy AsyncGroq singleton so the next call_groq() rebuilds it
    bound to the current event loop.

    Each Celery task invocation (``app.workers.tasks.run_research_task``)
    runs the async research graph under its own fresh ``asyncio.run(...)``
    event loop. AsyncGroq wraps an httpx.AsyncClient internally, which
    raises "RuntimeError: Event loop is closed" if reused inside a new loop
    after the one it was created in has closed. The task calls this before
    its own ``asyncio.run`` so ``_get_client`` rebuilds it fresh. Mirrors
    ``app/db/session.py::reset_session_factory`` exactly — drop the
    reference, let the existing lazy-init path recreate it.
    """
    global _client
    _client = None


@dataclass(frozen=True)
class GroqResult:
    """Return shape for ``call_groq``: completion text plus usage.

    ``usage_metadata`` is a real field rather than a computed property
    because LangSmith reads token counts off the *serialized return value*
    of a ``@traceable``-decorated function, and a property would not appear
    in that serialization. Its three keys (``input_tokens``,
    ``output_tokens``, ``total_tokens``) are the LangSmith-recognized names
    for rendering token counts on a trace automatically.

    RESEARCH.md Assumption A2: whether LangSmith's serializer surfaces this
    field from a dataclass exactly as it does from a plain dict is verified
    manually in plan 12-14. If it does not, the fallback is a plain-dict
    return shape — ``prompt_tokens``/``completion_tokens`` remain the
    authoritative source for OBS-02 regardless, since they feed the
    ``AgentOutput`` database columns, not the LangSmith UI.
    """

    text: str
    prompt_tokens: int
    completion_tokens: int
    usage_metadata: dict[str, int]


def _build_groq_result(text: str, usage: object | None) -> GroqResult:
    """Build a ``GroqResult`` from completion text and a Groq usage object.

    Defensively extracts token counts (RESEARCH.md Pitfall 4): ``usage`` is
    bound to a local first and only read from if truthy, defaulting each
    count to 0. Never chain the attribute access straight off the response's
    usage attribute unguarded — the installed SDK types ``usage`` as
    optional, and an error-adjacent or streaming-shaped response would
    otherwise raise an AttributeError from inside every agent node.
    """
    prompt_tokens = usage.prompt_tokens if usage else 0  # type: ignore[attr-defined]
    completion_tokens = usage.completion_tokens if usage else 0  # type: ignore[attr-defined]
    return GroqResult(
        text=text,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        usage_metadata={
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )


@traceable(run_type="llm", name="groq_chat_completion")
async def call_groq(
    prompt: str,
    model: str = "llama-3.3-70b-versatile",
    max_tokens: int = 1024,
) -> GroqResult:
    """Perform a real, rate-limited Groq chat-completion call.

    Acquires *max_tokens* from the shared rate limiter before making any
    request to the Groq API, then sends a single-turn chat completion and
    returns a ``GroqResult`` carrying the response text plus its usage.
    Groq SDK errors (APIConnectionError, RateLimitError, APIStatusError) are
    not caught here — they propagate to the caller; the SDK's own
    retry/backoff plus the rate limiter above already cover transient
    failures, so no hand-rolled retry loop is added.

    This function is decorated with LangSmith's ``@traceable`` (run_type
    "llm"), so every Groq call in the codebase emits a trace when tracing is
    enabled. Tracing is optional and off by default: it activates only when
    the LangSmith environment variables (``LANGSMITH_TRACING``,
    ``LANGSMITH_API_KEY``) are set — the decorator itself checks these at
    call time and executes this function normally, with no network call to
    LangSmith, when they are unset. When tracing IS enabled, the full prompt
    text and completion text of every agent call leave this application's
    infrastructure and are sent to LangSmith's hosted backend — enable only
    with that data-exposure implication in mind.

    Args:
        prompt:     The prompt text to send to Groq.
        model:      Groq model identifier (default: llama-3.3-70b-versatile).
        max_tokens: Token budget to reserve from the rate limiter and pass
                    to the Groq API as the completion's max_tokens.

    Returns:
        A ``GroqResult`` whose ``text`` field carries what this function
        used to return directly, plus ``prompt_tokens``/``completion_tokens``
        and a LangSmith-shaped ``usage_metadata`` dict.
    """
    await groq_rate_limiter.acquire(max_tokens)
    client = _get_client(get_settings().GROQ_API_KEY)
    response = await client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        max_tokens=max_tokens,
    )
    return _build_groq_result(response.choices[0].message.content, response.usage)
