"""Unit tests for the FRED HTTP client.

Tests verify:
  - fred_client module-level singleton exists
  - get_series_observations returns normalized {date, value} dicts
  - FRED's missing-value marker ('.') rows are dropped
  - api_key is sourced lazily from FRED_API_KEY at call time
  - an empty/unset key raises ValueError before any network use
  - MACRO_SERIES contains the canonical macro/sector series ids

Mirrors tests/services/test_edgar_client.py's httpx-mock boundary-test
convention — no live network calls.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.fred_client import (
    FRED_BASE_URL,
    MACRO_SERIES,
    FredClient,
    fred_client,
)

# ---------------------------------------------------------------------------
# Canned FRED response payload
# ---------------------------------------------------------------------------

_CANNED_OBSERVATIONS_PAYLOAD: dict = {
    "observations": [
        {"date": "2026-06-01", "value": "5.33"},
        {"date": "2026-05-01", "value": "."},  # FRED missing-value marker
        {"date": "2026-04-01", "value": "5.31"},
    ]
}


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def test_fred_client_singleton_exists() -> None:
    """fred_client module-level singleton is a FredClient instance."""
    assert fred_client is not None
    assert isinstance(fred_client, FredClient)


# ---------------------------------------------------------------------------
# MACRO_SERIES canonical set
# ---------------------------------------------------------------------------


def test_macro_series_contains_canonical_series() -> None:
    """MACRO_SERIES maps the 4 macro/sector series MacroSector needs."""
    series_ids = set(MACRO_SERIES.values())
    assert "FEDFUNDS" in series_ids
    assert "CPIAUCSL" in series_ids
    assert "DGS10" in series_ids
    assert "UNRATE" in series_ids


# ---------------------------------------------------------------------------
# get_series_observations — normalization + missing-value drop
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_series_observations_normalizes_and_drops_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns normalized {date, value} dicts, dropping the '.' missing row."""
    monkeypatch.setenv("FRED_API_KEY", "test-key-123")
    client = FredClient()

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_CANNED_OBSERVATIONS_PAYLOAD)

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url=FRED_BASE_URL,
    )

    result = await client.get_series_observations("FEDFUNDS")
    await client.close()

    assert result == [
        {"date": "2026-06-01", "value": "5.33"},
        {"date": "2026-04-01", "value": "5.31"},
    ]


@pytest.mark.anyio
async def test_get_series_observations_sends_api_key_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """api_key query param is populated from the monkeypatched FRED_API_KEY."""
    monkeypatch.setenv("FRED_API_KEY", "test-key-456")
    client = FredClient()

    sent_params: dict[str, str] = {}

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        sent_params.update(dict(request.url.params))
        return httpx.Response(200, json=_CANNED_OBSERVATIONS_PAYLOAD)

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url=FRED_BASE_URL,
    )

    await client.get_series_observations("FEDFUNDS")
    await client.close()

    assert sent_params.get("api_key") == "test-key-456"
    assert sent_params.get("series_id") == "FEDFUNDS"


# ---------------------------------------------------------------------------
# Empty key raises before any network use
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_empty_api_key_raises_value_error_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty/unset FRED_API_KEY raises ValueError before any request."""
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    client = FredClient()

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call should be made with an empty key")

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url=FRED_BASE_URL,
    )

    with pytest.raises(ValueError, match="FRED_API_KEY not set"):
        await client.get_series_observations("FEDFUNDS")

    await client.close()


# ---------------------------------------------------------------------------
# increment_api_call_count instrumentation (OBS-02, D-05, 12-07-PLAN.md)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_series_observations_increments_counter_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One get_series_observations call that issues a request awaits
    increment_api_call_count exactly once."""
    monkeypatch.setenv("FRED_API_KEY", "test-key-123")
    client = FredClient()

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_CANNED_OBSERVATIONS_PAYLOAD)

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url=FRED_BASE_URL,
    )

    with patch(
        "app.services.fred_client.increment_api_call_count", new_callable=AsyncMock
    ) as mock_increment:
        await client.get_series_observations("FEDFUNDS")

    await client.close()

    mock_increment.assert_awaited_once()


@pytest.mark.anyio
async def test_three_sequential_calls_award_three_increments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three sequential get_series_observations calls award three
    increments — the per-series loop is counted per call, not per method
    definition."""
    monkeypatch.setenv("FRED_API_KEY", "test-key-123")
    client = FredClient()

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_CANNED_OBSERVATIONS_PAYLOAD)

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url=FRED_BASE_URL,
    )

    with patch(
        "app.services.fred_client.increment_api_call_count", new_callable=AsyncMock
    ) as mock_increment:
        await client.get_series_observations("FEDFUNDS")
        await client.get_series_observations("CPIAUCSL")
        await client.get_series_observations("DGS10")

    await client.close()

    assert mock_increment.await_count == 3


@pytest.mark.anyio
async def test_missing_key_path_awards_zero_increments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call short-circuited by a missing API key awards zero increments."""
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    client = FredClient()

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call should be made with an empty key")

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url=FRED_BASE_URL,
    )

    with patch(
        "app.services.fred_client.increment_api_call_count", new_callable=AsyncMock
    ) as mock_increment:
        with pytest.raises(ValueError, match="FRED_API_KEY not set"):
            await client.get_series_observations("FEDFUNDS")

    await client.close()

    mock_increment.assert_not_awaited()


@pytest.mark.anyio
async def test_return_value_unchanged_when_counter_backend_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing counter backend (e.g. Redis down) never changes
    get_series_observations's return value — increment_api_call_count is
    fail-soft by construction (plan 12-03)."""
    from app.services.api_call_counter import set_current_plan_id

    monkeypatch.setenv("FRED_API_KEY", "test-key-123")
    set_current_plan_id("plan-counter-fail-fred")

    client = FredClient()

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_CANNED_OBSERVATIONS_PAYLOAD)

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url=FRED_BASE_URL,
    )

    try:
        with patch(
            "app.services.api_call_counter._redis",
            side_effect=ConnectionError("redis down"),
        ):
            result = await client.get_series_observations("FEDFUNDS")
    finally:
        set_current_plan_id(None)

    await client.close()

    assert result == [
        {"date": "2026-06-01", "value": "5.33"},
        {"date": "2026-04-01", "value": "5.31"},
    ]


# ---------------------------------------------------------------------------
# Async context manager protocol
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_async_context_manager_closes_on_exit() -> None:
    """__aexit__ closes the underlying httpx.AsyncClient."""
    client = FredClient()
    client.close = AsyncMock()

    async with client:
        pass

    client.close.assert_called_once()


# ---------------------------------------------------------------------------
# reset_fred_client — event-loop safety across Celery task boundaries
# ---------------------------------------------------------------------------


def test_reset_fred_client_replaces_the_httpx_client() -> None:
    """reset_fred_client() swaps fred_client's internal httpx.AsyncClient for
    a fresh one, without replacing the fred_client singleton object.

    Each Celery task runs the async research graph under its own fresh
    asyncio.run(...) event loop (same rationale as
    app/db/session.py::reset_session_factory). An httpx.AsyncClient opened
    inside a prior task's now-closed event loop raises "RuntimeError: Event
    loop is closed" if reused inside a new loop. This reset is synchronous so
    Celery's sync entrypoint can call it before any loop exists.
    """
    import app.services.fred_client as mod

    original_singleton_id = id(mod.fred_client)
    original_client = mod.fred_client._client

    mod.reset_fred_client()

    assert (
        id(mod.fred_client) == original_singleton_id
    ), "reset_fred_client must not replace the module-level singleton object"
    assert (
        mod.fred_client._client is not original_client
    ), "reset_fred_client must replace the underlying httpx.AsyncClient"
    assert isinstance(
        mod.fred_client._client, httpx.AsyncClient
    ), "the replacement must itself be an httpx.AsyncClient"
    assert (
        str(mod.fred_client._client.base_url).rstrip("/") == FRED_BASE_URL
    ), "the replacement client must be bound to FRED_BASE_URL"
