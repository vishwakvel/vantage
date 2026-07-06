"""Unit tests for the NewsAPI HTTP client.

Tests verify:
  - news_client module-level singleton exists
  - get_recent_articles normalizes NewsAPI's ``articles`` payload
  - the X-Api-Key header is populated from a monkeypatched NEWS_API_KEY
  - calling with an empty NEWS_API_KEY raises ValueError before any
    network use
  - importing this module never triggers Settings or any network call
"""

import httpx
import pytest

from app.services.news_client import (
    NEWS_API_BASE_URL,
    NewsAPIClient,
    news_client,
)

# ---------------------------------------------------------------------------
# Fixtures / canned payloads
# ---------------------------------------------------------------------------

_CANNED_ARTICLES_PAYLOAD = {
    "status": "ok",
    "totalResults": 2,
    "articles": [
        {
            "source": {"id": "reuters", "name": "Reuters"},
            "title": "Company X beats earnings expectations",
            "description": "Q3 earnings exceeded analyst estimates.",
            "content": "Full article body here...",
            "url": "https://example.com/article1",
            "publishedAt": "2026-07-01T12:00:00Z",
        },
        {
            "source": {"id": None, "name": "Bloomberg"},
            "title": "Sector outlook remains mixed",
            "description": None,
            "content": None,
            "url": "https://example.com/article2",
            "publishedAt": "2026-07-02T08:30:00Z",
        },
    ],
}


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_news_client_singleton_exists() -> None:
    """news_client module-level singleton is a NewsAPIClient instance."""
    assert news_client is not None
    assert isinstance(news_client, NewsAPIClient)


def test_news_api_base_url() -> None:
    """NEWS_API_BASE_URL targets the NewsAPI v2 endpoint."""
    assert NEWS_API_BASE_URL == "https://newsapi.org/v2"


# ---------------------------------------------------------------------------
# get_recent_articles: normalization + header population
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_recent_articles_normalizes_payload(monkeypatch) -> None:
    """get_recent_articles returns normalized dicts with expected keys/count."""
    monkeypatch.setenv("NEWS_API_KEY", "test-key-123")

    client = NewsAPIClient()

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_CANNED_ARTICLES_PAYLOAD)

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url=NEWS_API_BASE_URL,
    )

    articles = await client.get_recent_articles("AAPL")
    await client.close()

    assert len(articles) == 2
    first = articles[0]
    assert first["title"] == "Company X beats earnings expectations"
    assert first["description"] == "Q3 earnings exceeded analyst estimates."
    assert first["content"] == "Full article body here..."
    assert first["url"] == "https://example.com/article1"
    assert first["source"] == "Reuters"
    assert first["published_at"] == "2026-07-01T12:00:00Z"

    # Tolerates missing/None fields
    second = articles[1]
    assert second["description"] is None
    assert second["source"] == "Bloomberg"


@pytest.mark.anyio
async def test_get_recent_articles_sends_api_key_header(monkeypatch) -> None:
    """The X-Api-Key header is populated from a monkeypatched NEWS_API_KEY."""
    monkeypatch.setenv("NEWS_API_KEY", "monkeypatched-secret-key")

    client = NewsAPIClient()

    sent_headers: dict[str, str] = {}

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        sent_headers.update(dict(request.headers))
        return httpx.Response(200, json={"articles": []})

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url=NEWS_API_BASE_URL,
    )

    await client.get_recent_articles("AAPL")
    await client.close()

    assert sent_headers.get("x-api-key") == "monkeypatched-secret-key"


@pytest.mark.anyio
async def test_get_recent_articles_empty_key_raises_before_network(monkeypatch) -> None:
    """An empty NEWS_API_KEY raises ValueError before any network use."""
    monkeypatch.delenv("NEWS_API_KEY", raising=False)

    client = NewsAPIClient()

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network call should never happen with empty key")

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url=NEWS_API_BASE_URL,
    )

    with pytest.raises(ValueError, match="NEWS_API_KEY not set"):
        await client.get_recent_articles("AAPL")

    await client.close()


# ---------------------------------------------------------------------------
# Import safety — no Settings, no eager validation, no network at import
# ---------------------------------------------------------------------------


def test_import_succeeds_with_no_news_api_key(monkeypatch) -> None:
    """Importing app.services.news_client never raises, even with no key set."""
    monkeypatch.delenv("NEWS_API_KEY", raising=False)
    import importlib

    import app.services.news_client as mod

    importlib.reload(mod)
    assert mod.news_client is not None


def test_no_settings_import() -> None:
    """news_client.py never imports app.core.config (avoids eager validation)."""
    import inspect

    import app.services.news_client as mod

    source = inspect.getsource(mod)
    assert "from app.core.config" not in source


def test_no_groq_import() -> None:
    """news_client.py never imports the groq SDK."""
    import inspect

    import app.services.news_client as mod

    source = inspect.getsource(mod)
    assert "import groq" not in source
    assert "from groq" not in source


# ---------------------------------------------------------------------------
# reset_news_client — event-loop safety across Celery task boundaries
# ---------------------------------------------------------------------------


def test_reset_news_client_replaces_the_httpx_client() -> None:
    """reset_news_client() swaps news_client's internal httpx.AsyncClient for
    a fresh one, without replacing the news_client singleton object itself.

    Each Celery task runs the async research graph under its own fresh
    asyncio.run(...) event loop (same rationale as
    app/db/session.py::reset_session_factory). An httpx.AsyncClient opened
    inside a prior task's now-closed event loop raises "RuntimeError: Event
    loop is closed" if reused inside a new loop.
    """
    import app.services.news_client as mod

    original_singleton_id = id(mod.news_client)
    original_client = mod.news_client._client

    mod.reset_news_client()

    assert id(mod.news_client) == original_singleton_id, (
        "reset_news_client must not replace the module-level singleton object"
    )
    assert mod.news_client._client is not original_client, (
        "reset_news_client must replace the underlying httpx.AsyncClient"
    )
    assert isinstance(mod.news_client._client, httpx.AsyncClient)
    assert str(mod.news_client._client.base_url) == NEWS_API_BASE_URL + "/"
