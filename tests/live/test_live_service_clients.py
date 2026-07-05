"""Live smoke tests against REAL external APIs (D-01 live-verification gate).

Phase 5 introduces four new external service clients: NewsAPI, arXiv, FRED,
and yfinance-backed comparables. Per D-01 — mocked tests alone missed 5
product-critical bugs in Phase 2 (wrong search params, wrong response field
names, wrong primary-document logic, a missing FK upsert, a chromadb
client/server version mismatch) that only surfaced against real
dependencies — each new client gets exactly one live smoke test here, making
ONE real call and asserting the concrete field names the corresponding agent
depends on.

The whole module is gated behind RUN_LIVE_TESTS=1 so ordinary `pytest` runs
(CI, local dev) stay fully hermetic and make zero external network calls by
default. Run explicitly, e.g.:

    RUN_LIVE_TESTS=1 NEWS_API_KEY=... FRED_API_KEY=... \\
        python -m pytest tests/live/test_live_service_clients.py -v

arXiv and yfinance/comparables need no API key and always run once the
module-level gate is open. NewsAPI and FRED additionally skip individually
if their respective key is unset, so a partial key set still exercises what
it can.
"""

import os
import re

import pytest

from app.services.arxiv_client import arxiv_client
from app.services.comparables_source import comparables_source
from app.services.fred_client import fred_client
from app.services.news_client import news_client

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="live API tests: set RUN_LIVE_TESTS=1 to run",
    ),
]

# Mirrors app/services/comparables_source.py's own peer-ticker validation
# pattern — used here only to assert the shape of what get_peers returns,
# not to re-implement the client's validation.
_TICKER_PATTERN = re.compile(r"^[A-Z0-9]{1,10}$")


async def test_live_news_client_get_recent_articles() -> None:
    """NewsAPI: one real /everything call, asserting the shape SentimentNLP/Risk depend on."""
    if not os.environ.get("NEWS_API_KEY"):
        pytest.skip("NEWS_API_KEY not set — cannot run live NewsAPI smoke test")

    articles = await news_client.get_recent_articles("AAPL")

    assert isinstance(articles, list)
    assert len(articles) > 0
    first = articles[0]
    assert "title" in first
    assert "url" in first
    assert "published_at" in first


async def test_live_arxiv_client_search() -> None:
    """arXiv: one real /api/query call returns the shape SentimentNLP depends on."""
    papers = await arxiv_client.search("large language models")

    assert isinstance(papers, list)
    assert len(papers) > 0
    first = papers[0]
    assert "title" in first
    assert "abstract" in first
    assert "url" in first
    assert "published" in first


async def test_live_fred_client_get_series_observations() -> None:
    """FRED: one real /series/observations call returns the shape MacroSector depends on."""
    if not os.environ.get("FRED_API_KEY"):
        pytest.skip("FRED_API_KEY not set — cannot run live FRED smoke test")

    observations = await fred_client.get_series_observations("FEDFUNDS", limit=3)

    assert isinstance(observations, list)
    assert len(observations) > 0
    first = observations[0]
    assert "date" in first
    assert "value" in first


async def test_live_comparables_source_get_peers_and_metrics() -> None:
    """yfinance: real get_peers/get_metrics calls asserting the shapes ComparableCompanies needs."""
    peers = await comparables_source.get_peers("AAPL")

    assert isinstance(peers, list)
    for peer in peers:
        assert isinstance(peer, str)
        assert _TICKER_PATTERN.match(peer)

    metrics = await comparables_source.get_metrics(["MSFT"])

    assert isinstance(metrics, list)
    assert len(metrics) > 0
    assert "ticker" in metrics[0]
