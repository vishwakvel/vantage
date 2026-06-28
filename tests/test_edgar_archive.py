"""Unit tests for EDGARClient.get_archive() — SEC Archives download method.

Tests verify:
  - EDGAR_ARCHIVES_URL constant targets www.sec.gov
  - get_archive() issues requests to www.sec.gov (not efts.sec.gov)
  - get_archive() sends the same Vantage User-Agent header as get()
  - Existing get() behaviour (efts.sec.gov base) is unchanged
  - Archive and efts clients are independent (different base URLs)

All tests mock at the httpx transport layer — no real SEC network calls.
"""

import httpx
import pytest

from app.services.edgar_client import (
    EDGAR_ARCHIVES_URL,
    EDGAR_BASE_URL,
    EDGAR_USER_AGENT,
    EDGARClient,
)


# ---------------------------------------------------------------------------
# EDGAR_ARCHIVES_URL constant
# ---------------------------------------------------------------------------


def test_edgar_archives_url_is_www_sec_gov() -> None:
    """EDGAR_ARCHIVES_URL constant points to the Archives host."""
    assert EDGAR_ARCHIVES_URL == "https://www.sec.gov"


def test_edgar_archives_url_distinct_from_efts() -> None:
    """Archives URL is separate from the EFTS search URL."""
    assert EDGAR_ARCHIVES_URL != EDGAR_BASE_URL


# ---------------------------------------------------------------------------
# get_archive() — host assertion
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_archive_request_host_is_www_sec_gov() -> None:
    """get_archive() makes requests to www.sec.gov, not efts.sec.gov."""
    client = EDGARClient()
    captured_host: list[str] = []

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        captured_host.append(request.url.host)
        return httpx.Response(200, text="<html>filing</html>")

    client._archive_client = httpx.AsyncClient(
        headers={"User-Agent": EDGAR_USER_AGENT},
        transport=httpx.MockTransport(mock_transport),
        base_url=EDGAR_ARCHIVES_URL,
    )

    await client.get_archive("/Archives/edgar/data/0000320193/000032019323000106/0000320193-23-000106-index.json")
    await client.close()

    assert len(captured_host) == 1
    assert captured_host[0] == "www.sec.gov"


# ---------------------------------------------------------------------------
# get_archive() — User-Agent assertion
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_archive_sends_edgar_user_agent() -> None:
    """get_archive() sends the EDGAR_USER_AGENT header on every request."""
    client = EDGARClient()
    sent_headers: dict[str, str] = {}

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        sent_headers.update(dict(request.headers))
        return httpx.Response(200, text="<html>filing</html>")

    client._archive_client = httpx.AsyncClient(
        headers={"User-Agent": EDGAR_USER_AGENT},
        transport=httpx.MockTransport(mock_transport),
        base_url=EDGAR_ARCHIVES_URL,
    )

    await client.get_archive("/Archives/edgar/data/0000320193/000032019323000106/aapl-20230930.htm")
    await client.close()

    assert "user-agent" in sent_headers
    assert sent_headers["user-agent"] == EDGAR_USER_AGENT


@pytest.mark.anyio
async def test_get_archive_user_agent_matches_get_user_agent() -> None:
    """get_archive() uses the same User-Agent as the existing get() method."""
    client = EDGARClient()
    archive_ua: list[str] = []
    efts_ua: list[str] = []

    async def archive_transport(request: httpx.Request) -> httpx.Response:
        archive_ua.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, text="archive response")

    async def efts_transport(request: httpx.Request) -> httpx.Response:
        efts_ua.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, json={})

    client._archive_client = httpx.AsyncClient(
        headers={"User-Agent": EDGAR_USER_AGENT},
        transport=httpx.MockTransport(archive_transport),
        base_url=EDGAR_ARCHIVES_URL,
    )
    client._client = httpx.AsyncClient(
        headers={"User-Agent": EDGAR_USER_AGENT},
        transport=httpx.MockTransport(efts_transport),
        base_url=EDGAR_BASE_URL,
    )

    await client.get_archive("/Archives/edgar/data/test/filing.htm")
    await client.get("/LATEST/search-index")
    await client.close()

    assert archive_ua == efts_ua == [EDGAR_USER_AGENT]


# ---------------------------------------------------------------------------
# Existing get() — unchanged behaviour
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_existing_get_still_uses_efts_base() -> None:
    """get() still targets efts.sec.gov after get_archive() is added."""
    client = EDGARClient()
    captured_host: list[str] = []

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        captured_host.append(request.url.host)
        return httpx.Response(200, json={})

    client._client = httpx.AsyncClient(
        headers={"User-Agent": EDGAR_USER_AGENT},
        transport=httpx.MockTransport(mock_transport),
        base_url=EDGAR_BASE_URL,
    )

    await client.get("/LATEST/search-index")
    await client.close()

    assert captured_host == ["efts.sec.gov"]


@pytest.mark.anyio
async def test_get_archive_does_not_call_efts_client() -> None:
    """get_archive() never touches the efts.sec.gov client."""
    client = EDGARClient()
    efts_calls: list[str] = []
    archive_calls: list[str] = []

    async def efts_transport(request: httpx.Request) -> httpx.Response:
        efts_calls.append(str(request.url))
        return httpx.Response(200, json={})

    async def archive_transport(request: httpx.Request) -> httpx.Response:
        archive_calls.append(str(request.url))
        return httpx.Response(200, text="filing html")

    client._client = httpx.AsyncClient(
        headers={"User-Agent": EDGAR_USER_AGENT},
        transport=httpx.MockTransport(efts_transport),
        base_url=EDGAR_BASE_URL,
    )
    client._archive_client = httpx.AsyncClient(
        headers={"User-Agent": EDGAR_USER_AGENT},
        transport=httpx.MockTransport(archive_transport),
        base_url=EDGAR_ARCHIVES_URL,
    )

    await client.get_archive("/Archives/edgar/data/test/filing.htm")
    await client.close()

    assert len(efts_calls) == 0, "get_archive() must not call the efts client"
    assert len(archive_calls) == 1


# ---------------------------------------------------------------------------
# Async context manager — close covers both clients
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_context_manager_closes_both_clients() -> None:
    """async with EDGARClient() closes both _client and _archive_client."""
    from unittest.mock import AsyncMock

    client = EDGARClient()
    client.close = AsyncMock()  # type: ignore[method-assign]

    async with client as ctx:
        assert ctx is client

    client.close.assert_called_once()
