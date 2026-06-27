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


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a scoped ``AsyncSession``.

    Rolls back the session on exception; always closes it on exit.

    Example::

        async def route(session: AsyncSession = Depends(get_session)):
            result = await session.execute(select(User))
    """
    async with _get_session_factory()() as session:
        yield session
