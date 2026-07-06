"""NewsAPI HTTP client.

Fetches recent news articles for a ticker/query. Feeds SentimentNLP
(AGENT-01) as its primary signal and RiskAssessment (AGENT-02) as a
secondary signal for emerging risks (D-02/D-03).

All code that queries NewsAPI must go through this module. Direct HTTP
calls to NewsAPI outside this client are prohibited (app/services/ boundary
rule, PROJECT.md).

NEWS_API_KEY is read lazily from the process environment inside
``_api_key()`` — never at import time and never from
``app.core.config.Settings``. Settings validates all required fields
eagerly on instantiation, so reading the key from Settings here would force
that eager validation on every import of this module (same rationale
already documented in app/services/edgar_client.py for EDGAR_USER_AGENT and
app/services/groq_client.py's ``_get_client`` lazy-init pattern). Setting
NEWS_API_KEY has no effect on import — only on the first live call.
"""

import os
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# NewsAPI configuration constants
# ---------------------------------------------------------------------------

NEWS_API_BASE_URL: str = "https://newsapi.org/v2"


class NewsAPIClient:
    """Async HTTP client for NewsAPI's ``/everything`` endpoint.

    The API key is sent via the ``X-Api-Key`` header (never in the URL
    query string — T-05-SECRET mitigation) and is read lazily per-request
    from the environment, never cached at construction time.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=30.0,
            base_url=NEWS_API_BASE_URL,
        )

    def _api_key(self) -> str:
        """Return NEWS_API_KEY from the environment, read at call time."""
        return os.environ.get("NEWS_API_KEY", "")

    async def get_recent_articles(
        self, query: str, *, page_size: int = 20
    ) -> list[dict[str, Any]]:
        """Fetch recent news articles for *query* and normalize the results.

        Raises:
            ValueError: If NEWS_API_KEY is empty at call time. Never raised
                at import or construction — only when a real request would
                otherwise be attempted without a key.

        Returns:
            A list of normalized article dicts with keys: ``title``,
            ``description``, ``content``, ``url``, ``source``,
            ``published_at``. Missing upstream fields are tolerated via
            ``.get(...)``.
        """
        api_key = self._api_key()
        if not api_key:
            raise ValueError("NEWS_API_KEY not set")

        response = await self._client.get(
            "/everything",
            params={
                "q": query,
                "sortBy": "publishedAt",
                "language": "en",
                "pageSize": page_size,
            },
            headers={"X-Api-Key": api_key},
        )
        response.raise_for_status()
        payload = response.json()

        articles: list[dict[str, Any]] = []
        for article in payload.get("articles", []):
            source = article.get("source") or {}
            articles.append(
                {
                    "title": article.get("title"),
                    "description": article.get("description"),
                    "content": article.get("content"),
                    "url": article.get("url"),
                    "source": source.get("name"),
                    "published_at": article.get("publishedAt"),
                }
            )
        return articles

    async def close(self) -> None:
        """Close the underlying httpx.AsyncClient connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> "NewsAPIClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# Module-level singleton — import this; do NOT create additional instances
# ---------------------------------------------------------------------------

news_client = NewsAPIClient()


def reset_news_client() -> None:
    """Replace news_client's underlying httpx.AsyncClient with a fresh one.

    Each Celery task invocation (``app.workers.tasks.run_research_task``)
    runs the async research graph under its own fresh ``asyncio.run(...)``
    event loop (same rationale as
    ``app/db/session.py::reset_session_factory``). An httpx.AsyncClient
    opened inside a prior task's now-closed event loop raises "RuntimeError:
    Event loop is closed" if reused inside a new loop. The task calls this
    before its own ``asyncio.run`` so the client is rebuilt bound to the
    current loop.

    Unlike ``reset_session_factory``, this client is constructed eagerly at
    import time (not lazily), so there is no "drop the reference and let it
    rebuild on next use" shortcut — the replacement client is constructed
    here, directly.
    """
    news_client._client = httpx.AsyncClient(
        timeout=30.0,
        base_url=NEWS_API_BASE_URL,
    )
