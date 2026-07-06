"""arXiv HTTP client.

Fetches recent arXiv research abstracts for a query. Feeds SentimentNLP
(AGENT-01) as a secondary research-signal source (D-02).

All code that queries the arXiv API must go through this module. Direct
HTTP calls to arXiv outside this client are prohibited (app/services/
boundary rule, PROJECT.md).

arXiv's public API is unauthenticated — no API key logic is needed here,
unlike news_client.py's lazy NEWS_API_KEY sourcing.
"""

from typing import Any
from xml.etree import ElementTree

import httpx

# ---------------------------------------------------------------------------
# arXiv configuration constants
# ---------------------------------------------------------------------------

ARXIV_BASE_URL: str = "https://export.arxiv.org"

# arXiv's Atom feed namespace — must be registered to find elements by tag.
_ATOM_NS: str = "http://www.w3.org/2005/Atom"
_NS_MAP: dict[str, str] = {"atom": _ATOM_NS}


class ArxivClient:
    """Async HTTP client for the arXiv ``/api/query`` Atom search endpoint.

    Wraps a single httpx.AsyncClient, mirroring EDGARClient's structure.
    No API key is required — arXiv's public API is unauthenticated.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=30.0,
            base_url=ARXIV_BASE_URL,
        )

    async def search(self, query: str, *, max_results: int = 5) -> list[dict[str, Any]]:
        """Search arXiv for *query* and return normalized paper dicts.

        The arXiv API returns Atom XML, parsed here with the standard
        library ``xml.etree.ElementTree`` (no new third-party dependency).
        A zero-entry response returns an empty list; this method never
        raises on an empty feed.

        Returns:
            A list of dicts with keys: ``title``, ``abstract``, ``url``,
            ``published``.
        """
        response = await self._client.get(
            "/api/query",
            params={
                "search_query": f"all:{query}",
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": max_results,
            },
        )
        response.raise_for_status()

        root = ElementTree.fromstring(response.text)
        papers: list[dict[str, Any]] = []
        for entry in root.findall("atom:entry", _NS_MAP):
            title_el = entry.find("atom:title", _NS_MAP)
            summary_el = entry.find("atom:summary", _NS_MAP)
            id_el = entry.find("atom:id", _NS_MAP)
            published_el = entry.find("atom:published", _NS_MAP)

            title = (title_el.text or "").strip() if title_el is not None else ""
            abstract = (summary_el.text or "").strip() if summary_el is not None else ""
            url = (id_el.text or "").strip() if id_el is not None else ""
            published = (
                (published_el.text or "").strip() if published_el is not None else ""
            )

            papers.append(
                {
                    "title": title,
                    "abstract": abstract,
                    "url": url,
                    "published": published,
                }
            )
        return papers

    async def close(self) -> None:
        """Close the underlying httpx.AsyncClient connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> "ArxivClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# Module-level singleton — import this; do NOT create additional instances
# ---------------------------------------------------------------------------

arxiv_client = ArxivClient()


def reset_arxiv_client() -> None:
    """Replace arxiv_client's underlying httpx.AsyncClient with a fresh one.

    Each Celery task invocation (``app.workers.tasks.run_research_task``)
    runs the async research graph under its own fresh ``asyncio.run(...)``
    event loop (same rationale as
    ``app/db/session.py::reset_session_factory``). An httpx.AsyncClient
    opened inside a prior task's now-closed event loop raises "RuntimeError:
    Event loop is closed" if reused inside a new loop. The task calls this
    before its own ``asyncio.run`` so the client is rebuilt bound to the
    current loop.
    """
    arxiv_client._client = httpx.AsyncClient(
        timeout=30.0,
        base_url=ARXIV_BASE_URL,
    )
