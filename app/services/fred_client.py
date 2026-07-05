"""FRED (Federal Reserve Economic Data) HTTP client.

Feeds the MacroSector agent (AGENT-03, D-04) with recent observations for
macro/sector series (rates, inflation, sector indicators).

All code that queries the FRED API must go through this module — no direct
httpx/requests calls to FRED outside this client (app/services/ boundary
rule, PROJECT.md).

FRED_API_KEY is read lazily from the process environment inside a method,
never at import time and never via app.core.config.Settings — mirrors
groq_client._get_client's lazy-key pattern and edgar_client's avoidance of
Settings at import time (Settings eagerly validates unrelated required
fields like DATABASE_URL/JWT_SECRET_KEY).
"""

import os
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# FRED configuration constants
# ---------------------------------------------------------------------------

FRED_BASE_URL: str = "https://api.stlouisfed.org/fred"

# FRED's documented sentinel for a missing/unavailable observation value.
_MISSING_VALUE_MARKER: str = "."

# Canonical macro/sector series ids MacroSector queries — the single source
# of truth for which FRED series this phase's agent context is built from.
MACRO_SERIES: dict[str, str] = {
    "federal_funds_rate": "FEDFUNDS",
    "cpi": "CPIAUCSL",
    "10_year_treasury": "DGS10",
    "unemployment_rate": "UNRATE",
}


class FredClient:
    """Async HTTP client for the FRED economic-data API.

    Mirrors EDGARClient: one internal httpx.AsyncClient, a module-level
    singleton below, and async context-manager support. Unlike EDGAR, FRED
    requires an api_key query parameter rather than a header — the key is
    sourced lazily via _api_key() at call time, never at import.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=30.0,
            base_url=FRED_BASE_URL,
        )

    def _api_key(self) -> str:
        """Return the FRED API key from the environment, read lazily.

        Deliberately NOT read at import time and NOT sourced from
        app.core.config.Settings — see module docstring.
        """
        return os.environ.get("FRED_API_KEY", "")

    async def get_series_observations(
        self, series_id: str, *, limit: int = 12
    ) -> list[dict[str, Any]]:
        """Fetch recent observations for a FRED series.

        Args:
            series_id: A FRED series id (e.g. one of MACRO_SERIES's values).
            limit:     Maximum number of observations to return (default: 12).

        Returns:
            A list of {"date": ..., "value": ...} dicts, most recent first,
            with FRED's missing-value marker ('.') rows dropped.

        Raises:
            ValueError: If FRED_API_KEY is unset when a request is attempted.
        """
        api_key = self._api_key()
        if not api_key:
            raise ValueError("FRED_API_KEY not set")

        response = await self._client.get(
            "/series/observations",
            params={
                "series_id": series_id,
                "file_type": "json",
                "sort_order": "desc",
                "limit": limit,
                "api_key": api_key,
            },
        )
        response.raise_for_status()
        payload = response.json()

        observations: list[dict[str, Any]] = []
        for obs in payload.get("observations", []):
            if obs.get("value") == _MISSING_VALUE_MARKER:
                continue
            observations.append({"date": obs["date"], "value": obs["value"]})
        return observations

    async def close(self) -> None:
        """Close the underlying httpx.AsyncClient connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> "FredClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# Module-level singleton — import this; do NOT create additional instances
# ---------------------------------------------------------------------------

fred_client = FredClient()
