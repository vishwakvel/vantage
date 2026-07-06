"""EDGAR HTTP client.

User-Agent is mandatory per SEC EDGAR policy.  Every request MUST include
'Vantage/1.0 vishwak.vel@gmail.com' or EDGAR returns 429/403.

All code that queries the SEC EDGAR API must go through this module.
Direct HTTP calls to EDGAR outside this client are prohibited.

WR-01: EDGAR_USER_AGENT below is the single source of truth — it is NOT
sourced from app.core.config.Settings.  The module-level ``edgar_client``
singleton (bottom of this file) is constructed at import time, so reading it
from Settings would force get_settings() to run (and validate unrelated
required fields like DATABASE_URL/JWT_SECRET_KEY) on every import of this
module, including transitively via ingestion_service.  Setting an
``EDGAR_USER_AGENT`` environment variable has no effect on this client.
"""

from typing import Any

import httpx

# ---------------------------------------------------------------------------
# EDGAR configuration constants
# ---------------------------------------------------------------------------

EDGAR_USER_AGENT: str = "Vantage/1.0 vishwak.vel@gmail.com"
EDGAR_BASE_URL: str = "https://efts.sec.gov"

# SEC Archives host — different base URL from the EFTS search endpoint.
# Filing HTML content lives at https://www.sec.gov/Archives/edgar/data/…
# Use get_archive() to download from this host; do not call get() for Archives paths.
EDGAR_ARCHIVES_URL: str = "https://www.sec.gov"


class EDGARClient:
    """Async HTTP client for SEC EDGAR — both EFTS search and Archives download.

    Sets the required User-Agent header on every outbound request.  The
    header is configured at the httpx.AsyncClient level so it cannot be
    accidentally omitted by individual callers.

    Two internal clients are maintained:
      _client         — efts.sec.gov  (full-text search; used by get())
      _archive_client — www.sec.gov   (filing documents; used by get_archive())
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            headers={"User-Agent": EDGAR_USER_AGENT},
            timeout=30.0,
            base_url=EDGAR_BASE_URL,
        )
        self._archive_client: httpx.AsyncClient = httpx.AsyncClient(
            headers={"User-Agent": EDGAR_USER_AGENT},
            timeout=30.0,
            base_url=EDGAR_ARCHIVES_URL,
        )

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        """Send a GET request to the EDGAR full-text search API (efts.sec.gov).

        Args:
            path:    URL path relative to EDGAR_BASE_URL (e.g. '/LATEST/search-index').
            **kwargs: Passed through to httpx.AsyncClient.get().

        Returns:
            The httpx.Response from EDGAR.
        """
        return await self._client.get(path, **kwargs)

    async def get_archive(self, path: str, **kwargs: Any) -> httpx.Response:
        """Download a filing document from the SEC Archives (www.sec.gov).

        The Archives host is separate from the EFTS search host.  This method
        carries the same mandatory Vantage User-Agent header as get() to
        prevent 403/429 responses from the SEC rate limiter.

        Args:
            path:    URL path relative to EDGAR_ARCHIVES_URL, e.g.
                     '/Archives/edgar/data/{cik}/{accession}/{doc}'.
            **kwargs: Passed through to httpx.AsyncClient.get().

        Returns:
            The httpx.Response containing the raw filing document (HTML or text).
        """
        return await self._archive_client.get(path, **kwargs)

    async def close(self) -> None:
        """Close both underlying httpx.AsyncClient connection pools."""
        await self._client.aclose()
        await self._archive_client.aclose()

    async def __aenter__(self) -> "EDGARClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# Module-level singleton — import this; do NOT create additional instances
# ---------------------------------------------------------------------------

edgar_client: EDGARClient = EDGARClient()


def reset_edgar_client() -> None:
    """Replace BOTH of edgar_client's underlying httpx.AsyncClient instances
    with fresh ones.

    Each Celery task invocation (``app.workers.tasks.run_research_task``)
    runs the async research graph under its own fresh ``asyncio.run(...)``
    event loop (same rationale as
    ``app/db/session.py::reset_session_factory``). An httpx.AsyncClient
    opened inside a prior task's now-closed event loop raises "RuntimeError:
    Event loop is closed" if reused inside a new loop. The task calls this
    before its own ``asyncio.run`` so both clients are rebuilt bound to the
    current loop. EDGARClient maintains two independent clients (EFTS search
    and SEC Archives) — both must be replaced.
    """
    edgar_client._client = httpx.AsyncClient(
        headers={"User-Agent": EDGAR_USER_AGENT},
        timeout=30.0,
        base_url=EDGAR_BASE_URL,
    )
    edgar_client._archive_client = httpx.AsyncClient(
        headers={"User-Agent": EDGAR_USER_AGENT},
        timeout=30.0,
        base_url=EDGAR_ARCHIVES_URL,
    )
