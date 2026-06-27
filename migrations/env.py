"""Alembic migration environment — async-compatible (asyncpg / AsyncSession).

The DATABASE_URL is read from the environment at runtime and injected via
``config.set_main_option``; it is never hardcoded in alembic.ini (T-01-03-01).

``target_metadata`` is populated by importing the Base and all ORM models so
that Alembic autogenerate can detect all 9 domain tables.

This module is designed to be importable outside the Alembic CLI (e.g. for
tests / verification scripts that only need ``target_metadata``) — all
Alembic ``context.*`` access is guarded by a ``hasattr`` check so it is a
no-op unless executed by ``alembic``.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.db.models  # noqa: F401 — registers all ORM classes with Base.metadata

# Import Base and all models so autogenerate detects all 9 tables.
# Defining target_metadata here (before any context access) makes this
# module importable for standalone verification without the Alembic runtime.
from app.db.base import Base

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Everything below this line requires the Alembic CLI runtime.
# Guard with hasattr so direct Python imports (e.g. tests) don't fail.
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generate SQL without a DB connection)."""
    config = context.config
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Execute migrations against an active database connection."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations using run_sync."""
    config = context.config
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migration mode — drives the async engine."""
    asyncio.run(run_async_migrations())


if hasattr(context, "config"):
    # Running under the Alembic CLI — configure and execute migrations.
    config = context.config

    # Read DATABASE_URL from env var at runtime (never hardcoded) — T-01-03-01
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        config.set_main_option("sqlalchemy.url", database_url)

    if config.config_file_name is not None:
        fileConfig(config.config_file_name)

    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
