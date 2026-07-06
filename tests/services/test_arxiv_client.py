"""Unit tests for the arXiv HTTP client.

Tests verify:
  - arxiv_client module-level singleton exists
  - search() parses a canned 2-entry Atom XML response into normalized dicts
  - an empty <feed> (no entries) yields [] with no exception
  - importing this module never triggers any network call
"""

import httpx
import pytest

from app.services.arxiv_client import ARXIV_BASE_URL, ArxivClient, arxiv_client

# ---------------------------------------------------------------------------
# Canned Atom XML payloads
# ---------------------------------------------------------------------------

_CANNED_ATOM_TWO_ENTRIES = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2601.00001v1</id>
    <title>
      Sector Momentum in Equity Markets: A Transformer Approach
    </title>
    <summary>
      We study sector rotation dynamics using transformer architectures
      applied to historical price and volume data.
    </summary>
    <published>2026-01-01T00:00:00Z</published>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2601.00002v1</id>
    <title>Sentiment Signals from Earnings Call Transcripts</title>
    <summary>
      This paper explores extracting bullish/bearish sentiment signals
      from quarterly earnings call transcripts.
    </summary>
    <published>2026-01-02T00:00:00Z</published>
  </entry>
</feed>
"""

_CANNED_ATOM_EMPTY_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
</feed>
"""


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_arxiv_client_singleton_exists() -> None:
    """arxiv_client module-level singleton is an ArxivClient instance."""
    assert arxiv_client is not None
    assert isinstance(arxiv_client, ArxivClient)


def test_arxiv_base_url() -> None:
    """ARXIV_BASE_URL targets the arXiv export API."""
    assert ARXIV_BASE_URL == "https://export.arxiv.org"


# ---------------------------------------------------------------------------
# search(): Atom parsing
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_search_returns_normalized_papers() -> None:
    """search() returns 2 dicts with non-empty title/abstract/url/published."""
    client = ArxivClient()

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_CANNED_ATOM_TWO_ENTRIES.encode("utf-8"),
            headers={"content-type": "application/atom+xml"},
        )

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url=ARXIV_BASE_URL,
    )

    papers = await client.search("sector momentum")
    await client.close()

    assert len(papers) == 2
    for paper in papers:
        assert paper["title"]
        assert paper["abstract"]
        assert paper["url"]
        assert paper["published"]

    assert "Transformer" in papers[0]["title"]
    assert papers[1]["url"] == "http://arxiv.org/abs/2601.00002v1"
    assert papers[1]["published"] == "2026-01-02T00:00:00Z"


@pytest.mark.anyio
async def test_search_empty_feed_returns_empty_list() -> None:
    """An empty <feed> (no entries) yields [] with no exception raised."""
    client = ArxivClient()

    async def mock_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_CANNED_ATOM_EMPTY_FEED.encode("utf-8"),
            headers={"content-type": "application/atom+xml"},
        )

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_transport),
        base_url=ARXIV_BASE_URL,
    )

    papers = await client.search("nonexistent topic xyz123")
    await client.close()

    assert papers == []


# ---------------------------------------------------------------------------
# Boundary / import safety
# ---------------------------------------------------------------------------


def test_no_groq_import() -> None:
    """arxiv_client.py never imports the groq SDK."""
    import inspect

    import app.services.arxiv_client as mod

    source = inspect.getsource(mod)
    assert "import groq" not in source
    assert "from groq" not in source


def test_uses_stdlib_xml_parser() -> None:
    """arxiv_client.py parses Atom XML using the stdlib ElementTree module."""
    import inspect

    import app.services.arxiv_client as mod

    source = inspect.getsource(mod)
    assert "xml.etree" in source


# ---------------------------------------------------------------------------
# reset_arxiv_client — event-loop safety across Celery task boundaries
# ---------------------------------------------------------------------------


def test_reset_arxiv_client_replaces_the_httpx_client() -> None:
    """reset_arxiv_client() swaps arxiv_client's internal httpx.AsyncClient
    for a fresh one, without replacing the arxiv_client singleton object.

    Each Celery task runs the async research graph under its own fresh
    asyncio.run(...) event loop (same rationale as
    app/db/session.py::reset_session_factory). An httpx.AsyncClient opened
    inside a prior task's now-closed event loop raises "RuntimeError: Event
    loop is closed" if reused inside a new loop.
    """
    import httpx

    import app.services.arxiv_client as mod

    original_singleton_id = id(mod.arxiv_client)
    original_client = mod.arxiv_client._client

    mod.reset_arxiv_client()

    assert id(mod.arxiv_client) == original_singleton_id, (
        "reset_arxiv_client must not replace the module-level singleton object"
    )
    assert mod.arxiv_client._client is not original_client, (
        "reset_arxiv_client must replace the underlying httpx.AsyncClient"
    )
    assert isinstance(mod.arxiv_client._client, httpx.AsyncClient)
    assert str(mod.arxiv_client._client.base_url) == ARXIV_BASE_URL
