"""Integration tests for ``app.services.financial_metrics_service`` (09-04-PLAN.md).

These tests run against the real test-postgres instance (via the `db_session`
fixture), never a mocked session — ``ON CONFLICT DO UPDATE`` is
dialect-specific and its conflict-target index inference is resolved by
PostgreSQL itself, so a mocked session would assert nothing about whether
the upsert actually works (precisely the class of blind spot PROJECT.md
records for the Company-row FK gap).

Only the yfinance boundary
(``app.services.financial_metrics_service.financial_metrics_source.get_quarterly_metrics``)
is patched with an ``AsyncMock`` — everything downstream runs for real.

Coverage (mirrors 09-04-PLAN.md Task 2's <behavior> block, one test per case):
  - a brand-new ticker with no pre-seeded Company row persists successfully
    (the regression test for the PROJECT.md ⚠️ FK gap)
  - a second run with a changed value for one (metric_name, period) leaves a
    single row carrying the new value (no duplicates)
  - created_at is unchanged and updated_at advances across the two runs
  - the returned mapping is keyed by metric_name with (period, value) pairs
    in ascending period order
  - a period written by run 1 but absent from run 2's fetch still appears in
    run 2's returned mapping (the read-back-from-DB requirement)
  - an empty source result returns {} and writes zero rows
  - a forced failure inside the upsert leaves the outer transaction usable
    (the SAVEPOINT requirement)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company, FinancialMetric
from app.services.financial_metrics_service import persist_quarterly_metrics

pytestmark = pytest.mark.anyio

_PATCH_TARGET = (
    "app.services.financial_metrics_service.financial_metrics_source.get_quarterly_metrics"
)


async def test_brand_new_ticker_persists_without_fk_violation(
    db_session: AsyncSession,
) -> None:
    """No pre-seeded Company row — this is the PROJECT.md ⚠️ FK-gap regression test."""
    rows = [
        {"metric_name": "revenue", "period": "2024-03-31", "value": 100.0},
        {"metric_name": "net_income", "period": "2024-03-31", "value": 10.0},
    ]
    with patch(_PATCH_TARGET, AsyncMock(return_value=rows)):
        series = await persist_quarterly_metrics("NEWCO", db_session)

    assert series["revenue"] == [("2024-03-31", 100.0)]
    assert series["net_income"] == [("2024-03-31", 10.0)]

    company = await db_session.execute(select(Company).where(Company.ticker == "NEWCO"))
    assert company.scalar_one_or_none() is not None


async def test_reupsert_with_changed_value_leaves_single_row(
    db_session: AsyncSession,
) -> None:
    rows_v1 = [{"metric_name": "revenue", "period": "2024-03-31", "value": 100.0}]
    rows_v2 = [{"metric_name": "revenue", "period": "2024-03-31", "value": 200.0}]

    with patch(_PATCH_TARGET, AsyncMock(return_value=rows_v1)):
        await persist_quarterly_metrics("DUPCO", db_session)
    with patch(_PATCH_TARGET, AsyncMock(return_value=rows_v2)):
        series = await persist_quarterly_metrics("DUPCO", db_session)

    assert series["revenue"] == [("2024-03-31", 200.0)]

    count_result = await db_session.execute(
        select(func.count())
        .select_from(FinancialMetric)
        .where(
            FinancialMetric.ticker == "DUPCO",
            FinancialMetric.metric_name == "revenue",
            FinancialMetric.period == "2024-03-31",
        )
    )
    assert count_result.scalar_one() == 1


async def test_reupsert_advances_updated_at_keeps_created_at(
    db_session: AsyncSession,
) -> None:
    """Two separate research runs are two separate committed transactions in
    production — PostgreSQL's ``now()`` is fixed for the lifetime of a single
    transaction, so this test commits between runs to model that (a same-
    transaction double-call, which never happens in the real call path,
    would see the same ``now()`` for both upserts)."""
    rows_v1 = [{"metric_name": "revenue", "period": "2024-03-31", "value": 100.0}]
    rows_v2 = [{"metric_name": "revenue", "period": "2024-03-31", "value": 200.0}]

    # Column-only selects (not the ORM entity) deliberately avoid
    # SQLAlchemy's identity map: with expire_on_commit=False, re-selecting
    # the same mapped entity by PK returns the already-loaded in-memory
    # object rather than re-reading the row, which would make this
    # assertion pass or fail on stale cached values instead of the real ones.
    timestamp_cols = select(FinancialMetric.created_at, FinancialMetric.updated_at).where(
        FinancialMetric.ticker == "TSCO",
        FinancialMetric.metric_name == "revenue",
        FinancialMetric.period == "2024-03-31",
    )

    with patch(_PATCH_TARGET, AsyncMock(return_value=rows_v1)):
        await persist_quarterly_metrics("TSCO", db_session)
    await db_session.commit()

    created_at_v1, updated_at_v1 = (await db_session.execute(timestamp_cols)).one()

    await asyncio.sleep(0.05)

    with patch(_PATCH_TARGET, AsyncMock(return_value=rows_v2)):
        await persist_quarterly_metrics("TSCO", db_session)

    created_at_v2, updated_at_v2 = (await db_session.execute(timestamp_cols)).one()

    assert created_at_v2 == created_at_v1
    assert updated_at_v2 > updated_at_v1


async def test_returned_mapping_sorted_ascending_by_period(
    db_session: AsyncSession,
) -> None:
    rows = [
        {"metric_name": "revenue", "period": "2024-06-30", "value": 300.0},
        {"metric_name": "revenue", "period": "2024-03-31", "value": 100.0},
        {"metric_name": "revenue", "period": "2023-12-31", "value": 50.0},
    ]
    with patch(_PATCH_TARGET, AsyncMock(return_value=rows)):
        series = await persist_quarterly_metrics("SORTCO", db_session)

    assert series["revenue"] == [
        ("2023-12-31", 50.0),
        ("2024-03-31", 100.0),
        ("2024-06-30", 300.0),
    ]


async def test_earlier_period_survives_when_absent_from_later_fetch(
    db_session: AsyncSession,
) -> None:
    rows_run1 = [
        {"metric_name": "revenue", "period": "2023-12-31", "value": 50.0},
        {"metric_name": "revenue", "period": "2024-03-31", "value": 100.0},
    ]
    rows_run2 = [{"metric_name": "revenue", "period": "2024-06-30", "value": 300.0}]

    with patch(_PATCH_TARGET, AsyncMock(return_value=rows_run1)):
        await persist_quarterly_metrics("HISTCO", db_session)

    with patch(_PATCH_TARGET, AsyncMock(return_value=rows_run2)):
        series = await persist_quarterly_metrics("HISTCO", db_session)

    assert series["revenue"] == [
        ("2023-12-31", 50.0),
        ("2024-03-31", 100.0),
        ("2024-06-30", 300.0),
    ]


async def test_empty_source_returns_empty_mapping_and_writes_nothing(
    db_session: AsyncSession,
) -> None:
    with patch(_PATCH_TARGET, AsyncMock(return_value=[])):
        series = await persist_quarterly_metrics("EMPTYCO", db_session)

    assert series == {}

    company = await db_session.execute(select(Company).where(Company.ticker == "EMPTYCO"))
    assert company.scalar_one_or_none() is None

    metrics = await db_session.execute(
        select(func.count()).select_from(FinancialMetric).where(FinancialMetric.ticker == "EMPTYCO")
    )
    assert metrics.scalar_one() == 0


async def test_failed_upsert_leaves_outer_transaction_usable(
    db_session: AsyncSession,
) -> None:
    """A row flushed on the caller's session before the call survives a
    failed metrics write — proves the SAVEPOINT (not a plain rollback) is
    what backs the write."""
    company = Company(ticker="SAVEPT")
    db_session.add(company)
    await db_session.flush()

    # metric_name exceeds the VARCHAR(50) column limit — a real PostgreSQL-level
    # failure inside the upsert, not a mocked one.
    bad_rows = [{"metric_name": "x" * 51, "period": "2024-03-31", "value": 1.0}]
    with patch(_PATCH_TARGET, AsyncMock(return_value=bad_rows)):
        with pytest.raises(Exception):  # noqa: B017 - dialect-specific DBAPIError
            await persist_quarterly_metrics("SAVEPT", db_session)

    result = await db_session.execute(select(Company).where(Company.ticker == "SAVEPT"))
    assert result.scalar_one_or_none() is not None
