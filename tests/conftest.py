"""Pytest configuration and shared fixtures for Vantage integration tests.

Provides:
- anyio_backend: restricts anyio to asyncio (trio not installed)
- test_settings: in-memory Settings with test DB/Redis/JWT config
- db_session: per-function AsyncSession backed by test-postgres (port 5433)
- async_client: per-function httpx.AsyncClient with FastAPI dependency overrides

Test isolation strategy (D-03):
- db_session creates all tables at fixture start and drops all at fixture end.
- Each test function gets a clean schema.
- Real network calls are prohibited — Redis is mocked where needed; DB uses
  test-postgres on port 5433 (docker-compose.test.yml).
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401 — registers all ORM classes with Base.metadata
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "postgresql+asyncpg://vantage:vantage@localhost:5433/vantage_test"
TEST_REDIS_URL = "redis://localhost:6379/1"  # DB 1 isolates from dev (DB 0)
TEST_JWT_SECRET = "test-jwt-secret-not-for-production"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    """Restrict anyio to the asyncio backend (trio is not installed)."""
    return request.param


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Return a Settings instance configured for the test environment.

    Uses:
    - test-postgres on port 5433 (not the dev DB on 5432)
    - Redis DB 1 (not DB 0)
    - A deterministic JWT secret clearly labelled as test-only

    This fixture is session-scoped so it is created once and reused across
    all tests — Settings is immutable so sharing is safe.
    """
    return Settings(
        DATABASE_URL=TEST_DATABASE_URL,
        REDIS_URL=TEST_REDIS_URL,
        JWT_SECRET_KEY=TEST_JWT_SECRET,
        JWT_ALGORITHM="HS256",
        JWT_ACCESS_TOKEN_EXPIRE_SECONDS=86400,
    )


@pytest.fixture()
async def db_session(test_settings: Settings) -> AsyncSession:
    """Yield a per-test AsyncSession backed by test-postgres.

    Schema lifecycle:
    1. create_all — build all tables registered in Base.metadata
    2. yield session — test runs
    3. drop_all — tear down schema
    4. dispose engine — release connection pool

    The drop_all ensures tests never share state.  A fresh engine is created
    per function so no connection pool leaks across tests.
    """
    engine = create_async_engine(test_settings.DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture()
async def async_client(db_session: AsyncSession, test_settings: Settings) -> AsyncClient:
    """Yield an httpx.AsyncClient wired to the FastAPI app with test dependencies.

    Dependency overrides:
    - get_settings → returns test_settings (test DB/Redis/JWT config)
    - get_session → yields db_session (test-postgres session, no real DB connection
      from the session module's lazy singletons)

    The app is created fresh per function call so dependency_overrides do not
    bleed between tests.
    """
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: test_settings

    async def _override_session():
        yield db_session

    application.dependency_overrides[get_session] = _override_session

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        yield client
