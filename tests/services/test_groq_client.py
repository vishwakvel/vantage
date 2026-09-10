"""Unit tests for the Groq async token-bucket rate limiter and call_groq.

Tests verify:
  - AsyncTokenBucketRateLimiter.acquire() completes immediately on a full bucket
  - acquire() blocks (awaits) when tokens are exhausted — never raises, never drops
  - groq_rate_limiter module-level singleton exists with correct capacity
  - call_groq() performs a real (SDK-boundary-mocked) Groq chat-completion:
    it acquires the rate limiter before calling the SDK, returns a GroqResult
    carrying the completion text and usage, defaults to
    openai/gpt-oss-20b, and invokes the SDK with the expected
    messages/model/max_tokens.
  - GroqResult defensively extracts prompt/completion token counts from the
    response's usage field, defaulting to 0 when usage is None, and exposes
    a LangSmith-shaped usage_metadata dict.
"""

import asyncio
import dataclasses
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.groq_client as groq_client_module
from app.services.groq_client import (
    AsyncTokenBucketRateLimiter,
    GroqResult,
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


def _make_mock_usage(prompt_tokens: int = 7, completion_tokens: int = 3) -> MagicMock:
    """Build a mock Groq CompletionUsage object."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    return usage


def _make_mock_client(
    content: str = "mocked completion text",
    usage: MagicMock | None = "__default__",  # type: ignore[assignment]
) -> MagicMock:
    """Build a mock AsyncGroq client whose chat.completions.create returns *content*.

    *usage* configures the mocked response's ``usage`` attribute: pass
    ``None`` explicitly to simulate a response with no usage field, or omit
    it to get a default populated usage mock.
    """
    if usage == "__default__":
        usage = _make_mock_usage()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content=content))]
    mock_response.usage = usage
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    return mock_client


@pytest.fixture(autouse=True)
def _reset_client_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure every test starts with a clean module-level client singleton."""
    monkeypatch.setattr(groq_client_module, "_client", None, raising=False)


def test_call_groq_default_model_is_gpt_oss_20b() -> None:
    """call_groq's default model parameter is openai/gpt-oss-20b.

    llama-3.3-70b-versatile was decommissioned by Groq on 2026-08-16
    (Phase 13-06). openai/gpt-oss-20b is the current default.
    """
    import inspect

    sig = inspect.signature(call_groq)
    assert sig.parameters["model"].default == "openai/gpt-oss-20b"


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
    """call_groq returns a GroqResult whose text equals the completion content."""
    mock_client = _make_mock_client(content="hello from groq")
    monkeypatch.setattr(groq_client_module, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(groq_client_module, "AsyncGroq", lambda api_key: mock_client)

    result = await call_groq("test prompt", max_tokens=10)

    assert isinstance(result, GroqResult)
    assert result.text == "hello from groq"


@pytest.mark.anyio
async def test_call_groq_returns_prompt_and_completion_token_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GroqResult.prompt_tokens/.completion_tokens equal the mocked usage counts."""
    mock_client = _make_mock_client(usage=_make_mock_usage(prompt_tokens=42, completion_tokens=17))
    monkeypatch.setattr(groq_client_module, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(groq_client_module, "AsyncGroq", lambda api_key: mock_client)

    result = await call_groq("test prompt", max_tokens=10)

    assert result.prompt_tokens == 42
    assert result.completion_tokens == 17


@pytest.mark.anyio
async def test_call_groq_none_usage_yields_zero_token_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response whose usage is None yields zero counts, no AttributeError."""
    mock_client = _make_mock_client(usage=None)
    monkeypatch.setattr(groq_client_module, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(groq_client_module, "AsyncGroq", lambda api_key: mock_client)

    result = await call_groq("test prompt", max_tokens=10)

    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0


@pytest.mark.anyio
async def test_call_groq_usage_metadata_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """GroqResult.usage_metadata carries LangSmith-recognized keys."""
    mock_client = _make_mock_client(usage=_make_mock_usage(prompt_tokens=5, completion_tokens=2))
    monkeypatch.setattr(groq_client_module, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(groq_client_module, "AsyncGroq", lambda api_key: mock_client)

    result = await call_groq("test prompt", max_tokens=10)

    assert result.usage_metadata == {
        "input_tokens": 5,
        "output_tokens": 2,
        "total_tokens": 7,
    }


def test_groq_result_is_frozen() -> None:
    """GroqResult is a frozen dataclass — assigning to a field raises."""
    result = GroqResult(
        text="x", prompt_tokens=0, completion_tokens=0, usage_metadata={}
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.text = "y"  # type: ignore[misc]


@pytest.mark.anyio
async def test_call_groq_invokes_sdk_with_expected_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AsyncGroq client is invoked with messages/model/max_tokens as specified."""
    mock_client = _make_mock_client()
    monkeypatch.setattr(groq_client_module, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(groq_client_module, "AsyncGroq", lambda api_key: mock_client)

    await call_groq("what is the ticker?", model="openai/gpt-oss-20b", max_tokens=256)

    mock_client.chat.completions.create.assert_awaited_once_with(
        messages=[{"role": "user", "content": "what is the ticker?"}],
        model="openai/gpt-oss-20b",
        max_tokens=256,
    )


# ---------------------------------------------------------------------------
# LangSmith tracing — @traceable on call_groq, no-op when tracing is off
# ---------------------------------------------------------------------------


def test_call_groq_carries_langsmith_tracing() -> None:
    """call_groq is wrapped by LangSmith's @traceable decorator.

    Asserted via the decorator's own ``is_traceable_function`` marker check
    rather than by string-matching source, per the plan's Test 1 wording.
    """
    from langsmith.run_helpers import is_traceable_function

    assert is_traceable_function(call_groq) is True


@pytest.mark.anyio
async def test_call_groq_unaffected_when_langsmith_env_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With every LangSmith-related env var cleared, call_groq still returns
    the correct GroqResult and raises nothing."""
    for var in (
        "LANGSMITH_TRACING",
        "LANGSMITH_API_KEY",
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    mock_client = _make_mock_client(
        content="hello", usage=_make_mock_usage(prompt_tokens=1, completion_tokens=1)
    )
    monkeypatch.setattr(groq_client_module, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(groq_client_module, "AsyncGroq", lambda api_key: mock_client)

    result = await call_groq("test prompt", max_tokens=10)

    assert isinstance(result, GroqResult)
    assert result.text == "hello"


@pytest.mark.anyio
async def test_call_groq_no_outbound_langsmith_call_when_tracing_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With tracing env vars cleared, the mocked Groq SDK boundary is the
    only network interaction driven by the call — no LangSmith HTTP client
    is constructed or invoked."""
    for var in (
        "LANGSMITH_TRACING",
        "LANGSMITH_API_KEY",
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    mock_client = _make_mock_client()
    monkeypatch.setattr(groq_client_module, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(groq_client_module, "AsyncGroq", lambda api_key: mock_client)

    langsmith_client_calls: list[object] = []
    from langsmith import client as langsmith_client_module

    original_request = langsmith_client_module.Client.request_with_retries

    def _tracking_request(self: object, *args: object, **kwargs: object) -> object:
        langsmith_client_calls.append((args, kwargs))
        return original_request(self, *args, **kwargs)

    monkeypatch.setattr(
        langsmith_client_module.Client, "request_with_retries", _tracking_request
    )

    await call_groq("test prompt", max_tokens=10)

    assert langsmith_client_calls == []
    mock_client.chat.completions.create.assert_awaited_once()


def test_settings_has_no_langsmith_or_langchain_field() -> None:
    """app.core.config.Settings has no field whose name contains a LangSmith
    or LangChain marker (guards D-01 against a future regression)."""
    from app.core.config import Settings

    matches = [
        f
        for f in Settings.model_fields
        if "langsmith" in f.lower() or "langchain" in f.lower()
    ]
    assert matches == []


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
