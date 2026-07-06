"""Async SQLAlchemy engine and session factory.

Usage in FastAPI routes::

    from app.db.session import get_session

    async def my_route(session: AsyncSession = Depends(get_session)):
        ...

The engine and session factory are created lazily on first use so that importing
this module does not require ``DATABASE_URL`` to be present in the environment
(important for tooling, tests that override settings, and type-checker runs).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

# Lazy singletons — created once and reused for the lifetime of the process.
_engine = None
_session_factory = None


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return (creating if necessary) the module-level async session factory.

    The factory is keyed to the engine which reads ``DATABASE_URL`` from
    the environment.  Because creation is lazy, ``import app.db.session``
    will never fail due to a missing ``DATABASE_URL``.
    """
    global _engine, _session_factory  # noqa: PLW0603
    if _session_factory is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
        )
        _session_factory = async_sessionmaker(
            _engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


def reset_session_factory() -> None:
    """Drop the lazy ``_engine``/``_session_factory`` singletons.

    Each Celery task invocation (``app.workers.tasks.run_research_task``)
    runs the async research graph under its own fresh ``asyncio.run(...)``
    event loop. An async engine created inside a prior task's (now-closed)
    event loop cannot be reused inside a new loop — asyncpg connections are
    bound to the loop that opened them. The task calls this function before
    its own ``asyncio.run`` so the next ``session_scope()``/
    ``_get_session_factory()`` call rebuilds the engine bound to the
    current loop.

    It is safe to simply drop the reference rather than calling
    ``engine.dispose()``: the prior loop's connections were already released
    when that loop closed, so there is nothing left to explicitly dispose
    against a (now-closed) loop.

    Does not affect ``get_session``/``session_scope`` behavior beyond
    causing the next call to lazily rebuild the engine/factory.
    """
    global _engine, _session_factory  # noqa: PLW0603
    _engine = None
    _session_factory = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a scoped ``AsyncSession``.

    Rolls back the session on exception; always closes it on exit.

    Example::

        async def route(session: AsyncSession = Depends(get_session)):
            result = await session.execute(select(User))
    """
    async with _get_session_factory()() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Yield a fresh, independent ``AsyncSession`` for concurrent graph nodes.

    This is the session source for LangGraph agent nodes that run
    concurrently in the parallel fan-out (AGENT-05): each
    ``async with session_scope()`` produces its own independent session, so
    concurrent node writes never collide on one ``AsyncSession`` (a single
    ``AsyncSession`` shared across concurrent coroutines raises asyncpg's
    "another operation is in progress").

    It reuses the existing lazy ``_get_session_factory()``, so importing
    this module still never requires ``DATABASE_URL``.

    Deliberately NOT a FastAPI dependency — request routes keep using
    ``get_session``. This is for graph-node-local sessions only.

    Example::

        async with session_scope() as session:
            session.add(some_row)
            await session.commit()
    """
    async with _get_session_factory()() as session:
        yield session
