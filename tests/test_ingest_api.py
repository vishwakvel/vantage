"""Ingest API endpoint tests — POST /api/v1/ingest/ticker and POST /api/v1/ingest/pdf.

All tests use httpx.TestClient (sync) via create_app() with dependency overrides
to mock ingestion_service and get_current_user.  No real EDGAR / ChromaDB / DB calls.

Coverage:
- test_ingest_ticker_endpoint: 200 with filings_ingested/filings_cached/source_warnings
- test_ingest_ticker_invalid_ticker: 422 on bad ticker (Pydantic validation)
- test_ingest_pdf_requires_auth: 401 when no Bearer token supplied
- test_ingest_pdf_uses_principal_user_id: ingest_pdf called with authenticated user's id
- test_source_warning_is_non_fatal: source_warnings in IngestionResult → HTTP 200, not 500

STRIDE compliance:
- T-02-01: user_id sourced from get_current_user, not request body
- T-02-02: ticker validated 1-10 alphanumerics before service call
- T-02-03: source failures surface as 200 + source_warnings, never 500
"""

import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.dependencies import get_current_user
from app.db.session import get_session
from app.main import create_app
from app.services.ingestion_service import IngestionResult

# ---------------------------------------------------------------------------
# Minimal settings for tests (no real DB/Redis required)
# ---------------------------------------------------------------------------

_TEST_SETTINGS = Settings(
    DATABASE_URL="postgresql+asyncpg://test:test@localhost:5433/test",
    REDIS_URL="redis://localhost:6379/1",
    JWT_SECRET_KEY="test-jwt-secret",
    JWT_ALGORITHM="HS256",
    JWT_ACCESS_TOKEN_EXPIRE_SECONDS=86400,
)

# ---------------------------------------------------------------------------
# URL constants
# ---------------------------------------------------------------------------

TICKER_URL = "/api/v1/ingest/ticker"
PDF_URL = "/api/v1/ingest/pdf"

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

FAKE_USER_ID = str(uuid.uuid4())


def _make_fake_user() -> MagicMock:
    """Return a mock User ORM instance with a predictable id."""
    user = MagicMock()
    user.id = FAKE_USER_ID
    return user


async def _mock_session():
    """Async generator yielding a MagicMock session (no real DB connection).

    WR-07: this must itself be an async generator FUNCTION (not a regular
    function that returns an async generator object) — FastAPI's dependency
    injection only wraps async generator functions with the proper
    open/close-on-teardown behavior. A regular function returning the
    generator object would be called once and the returned AsyncGenerator
    object handed directly to route handlers as the "session" dependency
    value, instead of the yielded MagicMock.
    """
    yield MagicMock()


def _make_app_with_auth_override():
    """Create a TestClient whose get_current_user and get_session are mocked.

    Overrides:
    - get_current_user: returns a fake User with a predictable id
    - get_session: yields a MagicMock (no real DB)
    - get_settings: returns _TEST_SETTINGS (no env-var resolution)

    Returns:
        (app, TestClient) tuple — app exposed so tests can add extra overrides.
    """
    from app.core.config import get_settings  # local import to avoid circular issues

    application = create_app()
    application.dependency_overrides[get_current_user] = lambda: _make_fake_user()
    application.dependency_overrides[get_session] = _mock_session
    application.dependency_overrides[get_settings] = lambda: _TEST_SETTINGS
    return application, TestClient(application, raise_server_exceptions=True)


def _make_app_no_auth():
    """Create a TestClient with NO get_current_user override (requires real JWT).

    get_session and get_settings are still mocked so no real DB/env is needed.
    """
    from app.core.config import get_settings

    application = create_app()
    application.dependency_overrides[get_session] = _mock_session
    application.dependency_overrides[get_settings] = lambda: _TEST_SETTINGS
    return application, TestClient(application, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# test_ingest_ticker_endpoint (INGEST-01)
# ---------------------------------------------------------------------------


def test_ingest_ticker_endpoint():
    """POST /api/v1/ingest/ticker returns 200 with IngestionResult fields.

    Asserts:
    - HTTP 200
    - JSON body contains ticker, filings_ingested, filings_cached, source_warnings
    - ingest_ticker was called with the supplied ticker
    """
    app, client = _make_app_with_auth_override()

    mock_result = IngestionResult(
        ticker="AAPL",
        filings_ingested=3,
        filings_cached=1,
        source_warnings=[],
    )

    with patch(
        "app.api.v1.ingest.ingestion_service.ingest_ticker",
        new=AsyncMock(return_value=mock_result),
    ) as mock_ingest:
        resp = client.post(TICKER_URL, json={"ticker": "AAPL"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticker"] == "AAPL"
    assert body["filings_ingested"] == 3
    assert body["filings_cached"] == 1
    assert body["source_warnings"] == []
    mock_ingest.assert_awaited_once()


# ---------------------------------------------------------------------------
# test_ingest_ticker_invalid_ticker (T-02-02)
# ---------------------------------------------------------------------------


def test_ingest_ticker_invalid_ticker():
    """POST /api/v1/ingest/ticker with an invalid ticker returns 422.

    Tickers with special characters (SSRF path traversal) must be rejected
    by Pydantic validation BEFORE any service call is made.
    """
    app, client = _make_app_with_auth_override()

    with patch(
        "app.api.v1.ingest.ingestion_service.ingest_ticker",
        new=AsyncMock(),
    ) as mock_ingest:
        resp = client.post(TICKER_URL, json={"ticker": "../../etc"})

    assert resp.status_code == 422
    mock_ingest.assert_not_awaited()


# ---------------------------------------------------------------------------
# test_ingest_ticker_requires_auth (WR-03 — no JWT → 401/403)
# ---------------------------------------------------------------------------


def test_ingest_ticker_requires_auth():
    """POST /api/v1/ingest/ticker without a Bearer token returns 401/403.

    WR-03: the endpoint previously had no authentication dependency, letting
    any unauthenticated client trigger EDGAR searches/downloads and ChromaDB
    writes for an arbitrary ticker. FastAPI's HTTPBearer dependency raises 403
    (not 401) by default when the Authorization header is missing.
    """
    _app, client = _make_app_no_auth()

    with patch(
        "app.api.v1.ingest.ingestion_service.ingest_ticker",
        new=AsyncMock(),
    ) as mock_ingest:
        resp = client.post(TICKER_URL, json={"ticker": "AAPL"})

    assert resp.status_code in (401, 403), (
        f"Expected 401 or 403 for unauthenticated ticker ingest, got {resp.status_code}: {resp.text}"
    )
    mock_ingest.assert_not_awaited()


# ---------------------------------------------------------------------------
# test_ingest_pdf_requires_auth (T-02-01 — no JWT → 401)
# ---------------------------------------------------------------------------


def test_ingest_pdf_requires_auth():
    """POST /api/v1/ingest/pdf without a Bearer token returns 401.

    FastAPI's HTTPBearer dependency raises 403 (not 401) by default when the
    Authorization header is missing.  The test accepts both 401 and 403 as
    "unauthenticated" signals, per the FastAPI security scheme design.
    """
    _app, client = _make_app_no_auth()

    file_content = b"%PDF-1.4 fake content"
    resp = client.post(
        PDF_URL,
        files={"file": ("test.pdf", io.BytesIO(file_content), "application/pdf")},
        data={"ticker": "AAPL", "form_type": "10-K", "period_of_report": "2023-09-30"},
    )

    # HTTPBearer returns 403 when header is absent; 401 when token is invalid.
    assert resp.status_code in (401, 403), (
        f"Expected 401 or 403 for unauthenticated PDF upload, got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# test_ingest_pdf_uses_principal_user_id (INGEST-03, T-02-01)
# ---------------------------------------------------------------------------


def test_ingest_pdf_uses_principal_user_id():
    """POST /api/v1/ingest/pdf passes the authenticated user's id to ingest_pdf.

    The endpoint MUST NOT accept user_id from the request body.
    The user_id passed to ingest_pdf must equal the authenticated principal's id.
    """
    app, client = _make_app_with_auth_override()

    mock_result = IngestionResult(
        ticker="AAPL",
        filings_ingested=1,
        filings_cached=0,
        source_warnings=[],
    )

    file_content = b"%PDF-1.4 fake content"
    with patch(
        "app.api.v1.ingest.ingestion_service.ingest_pdf",
        new=AsyncMock(return_value=mock_result),
    ) as mock_ingest_pdf:
        resp = client.post(
            PDF_URL,
            files={"file": ("report.pdf", io.BytesIO(file_content), "application/pdf")},
            data={
                "ticker": "AAPL",
                "form_type": "10-K",
                "period_of_report": "2023-09-30",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticker"] == "AAPL"

    # Verify ingest_pdf was called with the authenticated user's id (from JWT),
    # NOT from any body field.
    mock_ingest_pdf.assert_awaited_once()
    call_kwargs = mock_ingest_pdf.call_args
    # user_id is the second positional arg or keyword 'user_id'
    called_user_id = (
        call_kwargs.kwargs.get("user_id")
        or (call_kwargs.args[1] if len(call_kwargs.args) > 1 else None)
    )
    assert called_user_id == FAKE_USER_ID, (
        f"ingest_pdf was called with user_id={called_user_id!r}, "
        f"expected authenticated user id={FAKE_USER_ID!r}"
    )


# ---------------------------------------------------------------------------
# test_source_warning_is_non_fatal (INGEST-04, T-02-03)
# ---------------------------------------------------------------------------


def test_source_warning_is_non_fatal():
    """Source failures surface as HTTP 200 with source_warnings, never 500.

    When ingest_ticker returns an IngestionResult with source_warnings
    (e.g. EDGAR is down), the endpoint must still return 200 — never 500.
    """
    app, client = _make_app_with_auth_override()

    mock_result = IngestionResult(
        ticker="MSFT",
        filings_ingested=0,
        filings_cached=0,
        source_warnings=["EDGAR search failed for MSFT: Connection timeout"],
    )

    with patch(
        "app.api.v1.ingest.ingestion_service.ingest_ticker",
        new=AsyncMock(return_value=mock_result),
    ):
        resp = client.post(TICKER_URL, json={"ticker": "MSFT"})

    assert resp.status_code == 200, (
        f"Expected 200 for warning-bearing result, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert len(body["source_warnings"]) == 1
    assert "EDGAR search failed" in body["source_warnings"][0]
    assert body["filings_ingested"] == 0
