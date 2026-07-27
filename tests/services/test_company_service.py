"""Integration tests for ``app.services.company_service`` (09-04-PLAN.md).

Runs against real test-postgres (``db_session`` fixture) — ``ON CONFLICT DO
NOTHING`` is dialect-specific, so a mocked session would assert nothing about
whether the upsert actually works or is truly idempotent.

Also proves ``app.services.ingestion_service`` delegates to this module's
``ensure_company_exists`` rather than holding a duplicated private copy
(Task 1's extraction).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.company_service as company_service
import app.services.ingestion_service as ingestion_service
from app.db.models import Company
from app.services.company_service import ensure_company_exists

pytestmark = pytest.mark.anyio


async def test_ensure_company_exists_creates_row_for_brand_new_ticker(
    db_session: AsyncSession,
) -> None:
    result = await db_session.execute(select(Company).where(Company.ticker == "FRESH"))
    assert result.scalar_one_or_none() is None

    await ensure_company_exists("FRESH", db_session)

    result = await db_session.execute(select(Company).where(Company.ticker == "FRESH"))
    assert result.scalar_one_or_none() is not None


async def test_ensure_company_exists_is_idempotent(db_session: AsyncSession) -> None:
    await ensure_company_exists("DBLCO", db_session)
    await ensure_company_exists("DBLCO", db_session)  # must not raise

    result = await db_session.execute(
        select(Company).where(Company.ticker == "DBLCO")
    )
    rows = result.scalars().all()
    assert len(rows) == 1


def test_ingestion_service_delegates_to_shared_helper() -> None:
    """Proves delegation rather than a duplicated body: the same function
    object, not a copy."""
    assert ingestion_service.ensure_company_exists is company_service.ensure_company_exists
