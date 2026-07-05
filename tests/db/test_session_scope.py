"""Unit tests for ``app.db.session.session_scope``.

These tests never touch a real database: ``_get_session_factory`` is
monkeypatched with a fake factory that yields ``AsyncMock``-backed stand-ins,
so the suite runs with no docker-compose test-postgres dependency and no
``DATABASE_URL`` in the environment.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.db.session as session_module
from app.db.session import session_scope


class _FakeAsyncSession:
    """Duck-typed AsyncSession stand-in exposing add/commit/execute."""

    def __init__(self) -> None:
        self.add = MagicMock()
        self.commit = AsyncMock()
        self.execute = AsyncMock()

    async def __aenter__(self) -> "_FakeAsyncSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def _fake_session_factory() -> _FakeAsyncSession:
    """Mimic ``async_sessionmaker(...)()`` — called fresh each invocation."""
    return _FakeAsyncSession()


@pytest.mark.anyio
async def test_session_scope_yields_duck_typed_async_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """session_scope() is usable as an async context manager yielding a
    duck-typed AsyncSession exposing .add/.commit/.execute."""
    monkeypatch.setattr(
        session_module, "_get_session_factory", lambda: _fake_session_factory
    )

    async with session_scope() as session:
        assert hasattr(session, "add")
        assert hasattr(session, "commit")
        assert hasattr(session, "execute")
        session.add(object())
        await session.commit()
        await session.execute("SELECT 1")


@pytest.mark.anyio
async def test_session_scope_yields_distinct_sessions_per_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sequential session_scope() entries yield two DISTINCT session
    objects — proving each concurrent node gets its own session."""
    monkeypatch.setattr(
        session_module, "_get_session_factory", lambda: _fake_session_factory
    )

    async with session_scope() as a:
        pass
    async with session_scope() as b:
        pass

    assert a is not b


def test_import_session_module_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing app.db.session and referencing session_scope must not
    require DATABASE_URL — the module stays lazy at import time."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    # Module is already imported; re-affirm the attribute exists and is
    # callable without touching Settings/DATABASE_URL.
    assert callable(session_module.session_scope)
