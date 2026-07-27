"""Company-row FK-parent upsert — the single canonical entry point every
ticker-keyed table must call before writing a row that carries a
``companies.ticker`` foreign key.

Every ``companies.ticker`` FK requires a parent ``Company`` row to already
exist. Ticker resolution only *matches against* a company name — it never
persists a row — so a brand-new ticker with no prior research has no
``Company`` row yet. Without this upsert, the first FK-dependent insert for
that ticker raises a ``ForeignKeyViolationError`` (PROJECT.md ⚠️
Company-row upsert gap, fixed 2026-07-03 for the ingestion path, extended
here to be the single shared entry point for every FK-dependent table).
Uses ``ON CONFLICT DO NOTHING`` so concurrent writers for the same new
ticker don't race.

Current callers: the ingestion pipeline (``app/services/ingestion_service.py``)
and the Phase 9 financial metrics service
(``app/services/financial_metrics_service.py``).

This is deliberately a leaf module: it imports nothing from any other
module under ``app/services/``, so it can be imported from both the
ingestion path (which transitively pulls in the EDGAR client, ChromaDB,
and the chunker) and the agent path without dragging that entire subsystem
into the FundamentalAnalysis agent's import graph or creating a circular
import route.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company


async def ensure_company_exists(ticker: str, session: AsyncSession) -> None:
    """Upsert a minimal ``Company`` row for *ticker* if one doesn't exist.

    Every FK-dependent table (``Document``, ``FinancialMetric``, and future
    tables such as Phase 10's ``Watchlist``) requires a ``Company`` row to
    already exist before an insert referencing ``companies.ticker`` can
    succeed. Ticker resolution only *matches against* a company name — it
    never persists a row — so a brand-new ticker with no prior research has
    no ``Company`` row yet. Without this, the first FK-dependent insert for
    that ticker raises a ``ForeignKeyViolationError``. Uses ``ON CONFLICT DO
    NOTHING`` so concurrent writers for the same new ticker don't race.
    """
    stmt = (
        pg_insert(Company)
        .values(ticker=ticker)
        .on_conflict_do_nothing(index_elements=["ticker"])
    )
    await session.execute(stmt)
