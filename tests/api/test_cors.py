"""Tests for CORS preflight behaviour driven by ALLOWED_ORIGINS (D-11, DEPLOY-02).

Does NOT use the shared async_client fixture — that fixture depends on
db_session, which would make a pure middleware test skip whenever Postgres
is down. Instead, create_app() is called directly and driven over the ASGI
transport, with the origins environment variable set via monkeypatch before
the app is constructed (environment variables take precedence over the .env
file in pydantic-settings, so this is deterministic in both a developer
environment with a populated .env and in CI).
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app

CONFIGURED_ORIGIN = "https://vantage.example.com"
OTHER_ORIGIN = "https://not-allowed.example.com"


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    """Restrict anyio to the asyncio backend (trio is not installed)."""
    return request.param


@pytest.fixture()
def _configured_origins_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_ORIGINS", CONFIGURED_ORIGIN)
    # Required fields with no defaults must be present for Settings() to construct.
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://vantage:vantage@localhost:5433/vantage_test"
    )
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-not-for-production")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key-not-for-production")


@pytest.mark.anyio
async def test_preflight_from_configured_origin_is_allowed(_configured_origins_env: None) -> None:
    """A preflight from a configured origin receives an echoed allow-origin header."""
    application = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        response = await client.options(
            "/health",
            headers={
                "Origin": CONFIGURED_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.headers.get("access-control-allow-origin") == CONFIGURED_ORIGIN


@pytest.mark.anyio
async def test_preflight_from_unconfigured_origin_is_denied(_configured_origins_env: None) -> None:
    """A preflight from an unconfigured origin receives no allow-origin header."""
    application = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        response = await client.options(
            "/health",
            headers={
                "Origin": OTHER_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.anyio
async def test_no_allow_credentials_header_in_either_direction(
    _configured_origins_env: None,
) -> None:
    """Neither an allowed nor a denied preflight ever carries allow-credentials."""
    application = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        allowed = await client.options(
            "/health",
            headers={
                "Origin": CONFIGURED_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        denied = await client.options(
            "/health",
            headers={
                "Origin": OTHER_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
    assert "access-control-allow-credentials" not in allowed.headers
    assert "access-control-allow-credentials" not in denied.headers
