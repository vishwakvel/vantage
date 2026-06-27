"""EDGAR HTTP client.

User-Agent is mandatory per SEC EDGAR policy.  Every request MUST include
'Vantage/1.0 vishwak.vel@gmail.com' or EDGAR returns 429/403.

All code that queries the SEC EDGAR API must go through this module.
Direct HTTP calls to EDGAR outside this client are prohibited.
"""

from typing import Any

import httpx

# ---------------------------------------------------------------------------
# EDGAR configuration constants
# ---------------------------------------------------------------------------

EDGAR_USER_AGENT: str = "Vantage/1.0 vishwak.vel@gmail.com"
EDGAR_BASE_URL: str = "https://efts.sec.gov"


class EDGARClient:
    """Async HTTP client for the SEC EDGAR full-text search API.

    Sets the required User-Agent header on every outbound request.  The
    header is configured at the httpx.AsyncClient level so it cannot be
    accidentally omitted by individual callers.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            headers={"User-Agent": EDGAR_USER_AGENT},
            timeout=30.0,
            base_url=EDGAR_BASE_URL,
        )

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        """Send a GET request to the EDGAR API.

        Args:
            path:    URL path relative to EDGAR_BASE_URL (e.g. '/search').
            **kwargs: Passed through to httpx.AsyncClient.get().

        Returns:
            The httpx.Response from EDGAR.
        """
        return await self._client.get(path, **kwargs)

    async def close(self) -> None:
        """Close the underlying httpx.AsyncClient connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> "EDGARClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# Module-level singleton — import this; do NOT create additional instances
# ---------------------------------------------------------------------------

edgar_client: EDGARClient = EDGARClient()
