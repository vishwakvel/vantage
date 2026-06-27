"""Unit tests for EDGAR HTTP client and section_constants module.

Tests verify:
  - EDGAR_USER_AGENT constant has the exact required value
  - EDGARClient sets User-Agent on every outbound request (mock assertion)
  - EDGARClient supports async context manager protocol
  - edgar_client module-level singleton exists
  - section_constants: all public constants are non-empty strings
  - Required section constants are importable and non-empty
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.edgar_client import (
    EDGAR_BASE_URL,
    EDGAR_USER_AGENT,
    EDGARClient,
    edgar_client,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_edgar_user_agent_exact_value() -> None:
    """EDGAR_USER_AGENT matches the exact string required by SEC EDGAR policy."""
    assert EDGAR_USER_AGENT == "Vantage/1.0 vishwak.vel@gmail.com"


def test_edgar_user_agent_contains_email() -> None:
    """EDGAR_USER_AGENT contains the contact email address."""
    assert "vishwak.vel@gmail.com" in EDGAR_USER_AGENT


def test_edgar_base_url_is_efts() -> None:
    """EDGAR_BASE_URL targets the EDGAR full-text search endpoint."""
    assert EDGAR_BASE_URL == "https://efts.sec.gov"


# ---------------------------------------------------------------------------
# EDGARClient singleton
# ---------------------------------------------------------------------------


def test_edgar_client_singleton_exists() -> None:
    """edgar_client module-level singleton is an EDGARClient instance."""
    assert edgar_client is not None
    assert isinstance(edgar_client, EDGARClient)


# ---------------------------------------------------------------------------
# User-Agent enforcement on every request
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_sends_user_agent_header() -> None:
    """EDGARClient.get() sends User-Agent header on every request."""
    client = EDGARClient()

    # Capture the actual request sent through the httpx transport layer
    sent_headers: dict[str, str] = {}

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        sent_headers.update(dict(request.headers))
        return httpx.Response(200, json={})

    # Replace the internal client's transport
    client._client = httpx.AsyncClient(
        headers={"User-Agent": EDGAR_USER_AGENT},
        transport=httpx.MockTransport(mock_transport),
        base_url=EDGAR_BASE_URL,
    )

    await client.get("/search")
    await client.close()

    assert "user-agent" in sent_headers
    assert sent_headers["user-agent"] == EDGAR_USER_AGENT


@pytest.mark.anyio
async def test_get_always_includes_user_agent_even_with_extra_headers() -> None:
    """User-Agent is present even when the caller passes additional headers."""
    client = EDGARClient()

    sent_headers: dict[str, str] = {}

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        sent_headers.update(dict(request.headers))
        return httpx.Response(200, json={})

    client._client = httpx.AsyncClient(
        headers={"User-Agent": EDGAR_USER_AGENT},
        transport=httpx.MockTransport(mock_transport),
        base_url=EDGAR_BASE_URL,
    )

    await client.get("/search", headers={"X-Custom": "value"})
    await client.close()

    # Both User-Agent and custom header are present
    assert sent_headers.get("user-agent") == EDGAR_USER_AGENT
    assert sent_headers.get("x-custom") == "value"


# ---------------------------------------------------------------------------
# Async context manager protocol
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_async_context_manager_returns_client() -> None:
    """EDGARClient supports 'async with' and returns itself from __aenter__."""
    client = EDGARClient()
    # Patch close to avoid actual connection
    client.close = AsyncMock()

    async with client as ctx:
        assert ctx is client

    # __aexit__ must call close()
    client.close.assert_called_once()


@pytest.mark.anyio
async def test_async_context_manager_closes_on_exit() -> None:
    """__aexit__ closes the underlying httpx.AsyncClient."""
    client = EDGARClient()
    client.close = AsyncMock()

    async with client:
        pass

    client.close.assert_called_once()


# ---------------------------------------------------------------------------
# section_constants: all public constants are non-empty strings
# ---------------------------------------------------------------------------


def test_section_constants_all_non_empty_strings() -> None:
    """Every public constant in section_constants is a non-empty string."""
    from app.ingestion import section_constants

    public_vars = {
        k: v
        for k, v in vars(section_constants).items()
        if not k.startswith("_") and not callable(v)
    }

    bad = [k for k, v in public_vars.items() if not isinstance(v, str) or len(v) == 0]
    assert not bad, f"Non-string or empty constants in section_constants: {bad}"


def test_section_constants_minimum_count() -> None:
    """section_constants defines at least 14 public constants."""
    from app.ingestion import section_constants

    public_vars = {
        k: v
        for k, v in vars(section_constants).items()
        if not k.startswith("_") and not callable(v)
    }
    assert len(public_vars) >= 14, f"Expected >=14 constants, got {len(public_vars)}"


def test_section_constants_required_imports() -> None:
    """Core section constants are importable from section_constants."""
    from app.ingestion.section_constants import (
        SECTION_BUSINESS,
        SECTION_CONTRADICTIONS,
        SECTION_FINANCIALS,
        SECTION_FUNDAMENTALS,
        SECTION_MDA,
        SECTION_RISKS,
        SECTION_RISK_FACTORS,
        SECTION_SYNTHESIS,
    )

    assert SECTION_RISK_FACTORS
    assert SECTION_MDA
    assert SECTION_BUSINESS
    assert SECTION_FINANCIALS
    assert SECTION_FUNDAMENTALS
    assert SECTION_SYNTHESIS
    assert SECTION_CONTRADICTIONS
    assert SECTION_RISKS


def test_section_constants_edgar_sections() -> None:
    """SEC EDGAR filing section constants have correct string values."""
    from app.ingestion.section_constants import (
        SECTION_BUSINESS,
        SECTION_FINANCIALS,
        SECTION_MDA,
        SECTION_RISK_FACTORS,
    )

    assert SECTION_RISK_FACTORS == "risk_factors"
    assert SECTION_MDA == "mda"
    assert SECTION_BUSINESS == "business"
    assert SECTION_FINANCIALS == "financials"


def test_section_constants_memo_sections() -> None:
    """Research memo output section constants have correct string values."""
    from app.ingestion.section_constants import (
        SECTION_CONTRADICTIONS,
        SECTION_FUNDAMENTALS,
        SECTION_SYNTHESIS,
    )

    assert SECTION_FUNDAMENTALS == "fundamentals"
    assert SECTION_SYNTHESIS == "synthesis"
    assert SECTION_CONTRADICTIONS == "contradictions"
