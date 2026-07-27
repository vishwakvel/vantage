"""Persistence/orchestration layer for quarterly fundamentals (METRIC-01).

This module composes the yfinance client (``financial_metrics_source.py``),
the Company FK upsert (``company_service.py``), and the ``FinancialMetric``
write — keeping ``financial_metrics_source.py`` a pure external-data client,
the same services-boundary split ``ingestion_service.py`` already models.

Rows are re-upserted on every research run rather than fetched once and
frozen (D-06): a bad fetch self-heals on the next run with no backfill job,
and repeated runs never duplicate a ``(ticker, metric_name, period)`` row.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.db.models import FinancialMetric
from app.services.company_service import ensure_company_exists
from app.services.financial_metrics_source import financial_metrics_source


async def persist_quarterly_metrics(
    ticker: str, session: AsyncSession
) -> dict[str, list[tuple[str, float]]]:
    """Fetch, upsert, and return the full persisted per-metric history for *ticker*.

    Flow:
      1. Fetch quarterly rows from ``financial_metrics_source``. If empty,
         return ``{}`` immediately — no reason to touch the database or
         create a Company row for a ticker that yielded no metrics.
      2. Inside a SAVEPOINT (``session.begin_nested()``), ensure the FK-parent
         Company row exists, then issue a single multi-row
         ``ON CONFLICT DO UPDATE`` upsert keyed by
         ``(ticker, metric_name, period)`` — the conflict target
         ``uq_financial_metrics_ticker_metric_period`` plan 09-02 declares.
         A failure here rolls back only to the SAVEPOINT, leaving the
         caller's outer transaction (which may already hold a flushed
         ``AgentTask`` row) intact and the session usable.
      3. Read the full persisted history back from the database — not the
         rows just fetched — because rows accumulate across runs (D-06) and
         the persisted table can hold a deeper history than any single
         fetch returns. This is the "own historical range" plan 09-05's
         isolation forest fits against.

    Does not call ``session.commit()`` — the caller owns the transaction
    boundary.

    Args:
        ticker:  Already-validated ticker symbol.
        session: SQLAlchemy async session for PostgreSQL writes.

    Returns:
        ``dict[metric_name, list[(period, value)]]``, each list sorted
        ascending by period. Empty dict when the source yielded no rows.
    """
    rows = await financial_metrics_source.get_quarterly_metrics(ticker)
    if not rows:
        return {}

    async with session.begin_nested():
        await ensure_company_exists(ticker, session)

        values = [
            {
                "ticker": ticker,
                "metric_name": row["metric_name"],
                "period": row["period"],
                "value": row["value"],
            }
            for row in rows
        ]
        stmt = pg_insert(FinancialMetric).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["ticker", "metric_name", "period"],
            set_={"value": stmt.excluded.value, "updated_at": func.now()},
        )
        await session.execute(stmt)

    history_result = await session.execute(
        select(
            FinancialMetric.metric_name,
            FinancialMetric.period,
            FinancialMetric.value,
        )
        .where(FinancialMetric.ticker == ticker)
        .order_by(FinancialMetric.metric_name, FinancialMetric.period)
    )

    series_by_metric: dict[str, list[tuple[str, float]]] = {}
    for metric_name, period, value in history_result.all():
        series_by_metric.setdefault(metric_name, []).append((period, value))

    return series_by_metric
