"""Migration smoke test — verifies alembic upgrade head creates all 9 domain tables.

Skipped automatically when test-postgres is unavailable (e.g. in CI without
docker-compose.test.yml or on developer machines that haven't started the
test-postgres container).

Usage:
    docker-compose -f docker-compose.test.yml up -d test-postgres
    pytest tests/db/test_migrations.py -v

Test isolation (D-03):
- Uses a separate test-postgres on port 5433, not the dev DB on 5432.
- Performs a downgrade-then-upgrade cycle to guarantee a clean state.
- psycopg2 (synchronous driver) is used for the connectivity check and table
  inspection — asyncpg is not needed here.
"""

from alembic import command
from alembic.config import Config

# ---------------------------------------------------------------------------
# Test-postgres connection parameters (matches docker-compose.test.yml)
# ---------------------------------------------------------------------------

SYNC_TEST_DB_URL = "postgresql://vantage:vantage@localhost:5433/vantage_test"

# migrations/env.py is unconditionally async (asyncio.run(run_async_migrations())),
# so alembic commands need an async-driver URL — the sync URL above is only for the
# raw psycopg2 connectivity check and post-migration table inspection below.
ASYNC_TEST_DB_URL = "postgresql+asyncpg://vantage:vantage@localhost:5433/vantage_test"

# All 9 domain tables created by the initial migration (plan 01-03)
EXPECTED_TABLES = frozenset(
    {
        "users",
        "companies",
        "documents",
        "document_chunks",
        "research_requests",
        "research_plans",
        "research_memos",
        "agent_tasks",
        "agent_outputs",
    }
)


def _test_db_available() -> bool:
    """Return True if test-postgres on port 5433 is reachable.

    Uses a short connect_timeout so the check fails fast when the container
    is not running rather than hanging for the default TCP timeout.

    psycopg2-binary may not be installed in all environments; if the import
    fails we skip rather than raising ImportError (returns False).
    """
    try:
        import psycopg2  # noqa: F401 — availability check only

        conn = psycopg2.connect(
            host="localhost",
            port=5433,
            user="vantage",
            password="vantage",
            dbname="vantage_test",
            connect_timeout=2,
        )
        conn.close()
        return True
    except ImportError:
        return False  # psycopg2-binary not installed — skip gracefully
    except Exception:
        return False  # Container not running — skip gracefully


def test_upgrade_creates_all_nine_tables() -> None:
    """alembic upgrade head creates all 9 domain tables on a fresh schema.

    Skipped when test-postgres is unavailable.
    """
    import pytest

    if not _test_db_available():
        pytest.skip("test-postgres not running (docker-compose.test.yml)")

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", ASYNC_TEST_DB_URL)

    # Ensure clean slate before upgrading. Alembic's downgrade chain depends on
    # every migration reversing cleanly, which isn't guaranteed to be re-runnable
    # back-to-back (FK drop ordering, a partially-applied prior run leaving
    # alembic_version stamped with the tables already gone, etc.) — dropping and
    # recreating the schema directly is unconditionally clean regardless of history.
    import psycopg2

    conn = psycopg2.connect(SYNC_TEST_DB_URL)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.close()
    finally:
        conn.close()

    command.upgrade(alembic_cfg, "head")

    # Inspect the schema via a raw psycopg2 connection
    import psycopg2

    conn = psycopg2.connect(SYNC_TEST_DB_URL)
    try:
        cur = conn.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        actual = {row[0] for row in cur.fetchall()}
        cur.close()
    finally:
        conn.close()

    missing = EXPECTED_TABLES - actual
    assert not missing, (
        f"alembic upgrade head did not create the following tables: {missing}\n"
        f"Tables found: {actual}"
    )
