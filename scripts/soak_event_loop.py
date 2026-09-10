"""DEBT-03 reproduction harness — the event-loop-closed soak (D-14 / D-16).

This proves that running N research tasks back-to-back inside ONE long-lived
process does not leave a stray "Event loop is closed" traceback behind. Each
Celery ``run_research`` task invocation resets every module-level async-client
singleton before its own ``asyncio.run(...)`` (the six-call reset block in
``app/workers/tasks.py``); this script exercises that block across N event-loop
generations — the FIRST run can never reproduce the defect, only a later loop
transition can.

This is NOT a pytest test. It never runs in CI. It needs a real Postgres, a
real Redis, and real external APIs (EDGAR, NewsAPI, FRED, Groq), and it burns
Groq quota on every run — which is exactly why it is a verification-time script
run by a human at phase verification (plan 13-06), not a permanent test.

Prerequisites:
    - The dev docker-compose stack's ``postgres`` and ``redis`` services must
      be running (``docker-compose up -d postgres redis``).
    - ``.env`` must be populated, including ``DATABASE_URL``, ``REDIS_URL``,
      ``JWT_SECRET_KEY``, ``GROQ_API_KEY``, ``NEWS_API_KEY`` and
      ``FRED_API_KEY`` — the research graph calls all of these.
    - Alembic migrations must be at head (``alembic upgrade head``).

Invocation:
    # Redirect into a log file so the operator has a greppable artifact:
    .venv/bin/python scripts/soak_event_loop.py --runs 4 --ticker AAPL \
        > soak.log 2>&1
    # Then confirm the on-disk count agrees with the printed "OCCURRENCES:" line:
    grep -c 'Event loop is closed' soak.log
    # Skip teardown to inspect the seeded rows afterwards:
    .venv/bin/python scripts/soak_event_loop.py --keep > soak.log 2>&1

The script prints a single ``OCCURRENCES: <n>`` verdict line and exits 1 when
that count is greater than zero.

This script is deliberately NOT collected by pytest (it lives outside
``tests/`` and defines no ``test_``-prefixed callable), NOT imported from any
application module, and NOT referenced from any CI configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import logging
import os
import pathlib
import sys
import traceback
from datetime import UTC, datetime

# Standalone-script bootstrap: make ``app`` importable when this file is run
# directly (``python scripts/soak_event_loop.py``), since Python puts the
# script's own directory on ``sys.path``, not the repo root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402 - imports below must follow the bootstrap

from app.core.security import hash_password  # noqa: E402
from app.db.models import (  # noqa: E402
    AgentTask,
    ResearchMemo,
    ResearchMemoStatus,
    ResearchPlan,
    ResearchRequest,
    User,
)
from app.db.session import session_scope  # noqa: E402
from app.services.company_service import ensure_company_exists  # noqa: E402
from app.workers.tasks import run_research_task  # noqa: E402

#: Four runs give three loop transitions. The defect never shows on the first
#: run (there is no prior closed loop yet); D-16 calls for roughly 3-5.
_DEFAULT_RUNS: int = 4
_DEFAULT_TICKER: str = "AAPL"

#: Clearly non-production identity — the ``.invalid`` TLD is reserved by
#: RFC 2606 and can never resolve to a real mailbox, so this row is never
#: mistaken for a real user.
_SOAK_EMAIL = "soak-event-loop@vantage.invalid"
_SOAK_PASSWORD = "soak-event-loop-password-not-a-real-secret"  # noqa: S105 - throwaway

#: The exact ``RuntimeError`` text that every ``reset_*()`` docstring quotes
#: (``app/services/{edgar,news,arxiv,fred,groq}_client.py``) and that
#: ``app/db/session.py::reset_session_factory`` exists to prevent. asyncio's
#: default exception handler renders a closed-loop error containing this
#: string; ``httpx.AsyncClient.__del__`` firing after its loop closed does the
#: same via ``sys.unraisablehook``. Match it character for character — a
#: paraphrase silently detects nothing.
_TARGET_PHRASE = "Event loop is closed"


class SoakFailure(AssertionError):
    """Raised when a soak assertion fails — caught once at the top level."""


class _PhraseCountingHandler(logging.Handler):
    """Logging handler that counts formatted records containing ``_TARGET_PHRASE``.

    asyncio reports closed-loop errors from its default exception handler
    through ``logging.getLogger("asyncio")`` (call_exception_handler ->
    ``logger.error(..., exc_info=...)``), so a logging handler catches every
    run-phase occurrence — including the phrase as it appears inside the
    formatted traceback text produced from ``exc_info``.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.hits: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            formatted = self.format(record)
        except Exception:  # noqa: BLE001 - a broken formatter must not abort the soak
            return
        if _TARGET_PHRASE in formatted:
            first_line = formatted.splitlines()[0] if formatted else ""
            self.hits.append(
                f"logging  logger={record.name} level={record.levelname} :: {first_line}"
            )


def _install_unraisable_counter(sink: list[str]) -> None:
    """Wrap ``sys.unraisablehook`` to count closed-loop teardown-phase errors.

    This is the GC / interpreter-teardown path: an ``httpx.AsyncClient.__del__``
    (or an asyncpg transport ``__del__``) firing after its event loop has
    closed raises inside ``__del__``, which never reaches ``logging`` — the
    interpreter routes it to ``sys.unraisablehook`` instead. These are exactly
    the two stray tracebacks Phase 6 recorded on the backlog. The wrapper
    always delegates to the previously installed hook, so nothing is swallowed.
    """
    previous_hook = sys.unraisablehook

    def _counting_hook(unraisable: object) -> None:
        exc_value = getattr(unraisable, "exc_value", None)
        err_msg = getattr(unraisable, "err_msg", None)
        obj = getattr(unraisable, "object", None)
        rendered_parts = [str(err_msg or ""), repr(exc_value), repr(obj)]
        if exc_value is not None:
            rendered_parts.append(
                "".join(
                    traceback.format_exception(type(exc_value), exc_value, exc_value.__traceback__)
                )
            )
        rendered = " ".join(rendered_parts)
        if _TARGET_PHRASE in rendered:
            sink.append(
                f"unraisable  {err_msg or type(exc_value).__name__} :: {exc_value!r} "
                f"(object={obj!r})"
            )
        previous_hook(unraisable)

    sys.unraisablehook = _counting_hook


def _emit_verdict(handler: _PhraseCountingHandler, unraisable_hits: list[str]) -> None:
    """Print the greppable ``OCCURRENCES:`` verdict and fail on a non-zero count."""
    all_hits = [*handler.hits, *unraisable_hits]
    total = len(all_hits)
    print("\n=== DEBT-03 verdict ===")
    print(f"OCCURRENCES: {total}")
    for hit in all_hits:
        print(f"  - {hit}")
    print(
        f"  cross-check on disk:  grep -c '{_TARGET_PHRASE}' <logfile>   "
        "# must agree with OCCURRENCES above"
    )
    _check(
        total == 0,
        (
            f"zero '{_TARGET_PHRASE}' occurrences across the soak "
            f"(logging + unraisable paths); got {total}. Per D-15 a residual "
            "occurrence is acceptable ONLY if proven benign: cross-check each hit "
            "above against the per-run memo terminal status and per-agent status "
            "table printed earlier, confirm no agent result was affected, and "
            "record the finding in 13-VERIFICATION.md."
        ),
    )


def _atexit_summary(handler: _PhraseCountingHandler, unraisable_hits: list[str]) -> None:
    """Flush a final count at interpreter shutdown.

    Interpreter-teardown occurrences can fire AFTER ``main()`` returns. A hit
    that appears in this line but NOT in the verdict block above is by
    definition a teardown-phase occurrence — the exact D-15 category.
    """
    total = len(handler.hits) + len(unraisable_hits)
    print(f"\n[atexit] final '{_TARGET_PHRASE}' occurrence count: {total}", file=sys.stderr)


def _check(condition: bool, description: str) -> None:
    """Assert-and-print helper — raises SoakFailure with context on failure."""
    if not condition:
        raise SoakFailure(f"FAILED: {description}")
    print(f"  [OK] {description}")


async def _seed_soak_fixtures(runs: int, ticker: str) -> tuple[str, str, list[str]]:
    """Idempotently seed the User -> Request -> Plan -> N PENDING Memos chain.

    Get-or-create the soak ``User`` by its constant email, ensure a ``Company``
    row exists for ``ticker`` (the memo's ``ticker`` FK targets
    ``companies.ticker``), create one ``ResearchRequest`` + one ``ResearchPlan``
    under it, then ``runs`` PENDING ``ResearchMemo`` rows sharing that plan —
    one per soak iteration, each created exactly the way
    ``app/api/v1/research.py`` creates its memo. Returns the plan id, the user
    id, and the list of memo ids, all as ``str``.
    """
    async with session_scope() as session:
        result = await session.execute(select(User).where(User.email == _SOAK_EMAIL))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(email=_SOAK_EMAIL, password_hash=hash_password(_SOAK_PASSWORD))
            session.add(user)
            await session.flush()

        await ensure_company_exists(ticker, session)

        request = ResearchRequest(
            user_id=user.id,
            raw_query=f"[soak] DEBT-03 event-loop soak for {ticker}",
            resolved_tickers=[ticker],
            status="COMPLETE",
        )
        session.add(request)
        await session.flush()

        plan = ResearchPlan(
            request_id=request.id,
            user_id=user.id,
            resolved_tickers=[ticker],
        )
        session.add(plan)
        await session.flush()

        memo_ids: list[str] = []
        for _ in range(runs):
            memo = ResearchMemo(
                plan_id=plan.id,
                user_id=user.id,
                ticker=ticker,
                status=ResearchMemoStatus.PENDING,
                body={},
                parent_memo_id=None,
            )
            session.add(memo)
            await session.flush()
            memo_ids.append(str(memo.id))

        await session.commit()
        return str(plan.id), str(user.id), memo_ids


async def _report_run(memo_id: str, plan_id: str, since: datetime) -> tuple[str, dict[str, str]]:
    """Re-read the memo's terminal status and this run's per-agent statuses.

    This is the D-15 evidence: it lets a residual traceback be proven not to
    affect any agent result. ``AgentTask`` rows carry no memo link, so this
    run's tasks are isolated by ``created_at >= since`` (the wall-clock instant
    captured just before the task was invoked).
    """
    async with session_scope() as session:
        memo = (
            await session.execute(select(ResearchMemo).where(ResearchMemo.id == memo_id))
        ).scalar_one()
        tasks = (
            (
                await session.execute(
                    select(AgentTask).where(
                        AgentTask.plan_id == plan_id,
                        AgentTask.created_at >= since,
                    )
                )
            )
            .scalars()
            .all()
        )
        agent_statuses = {task.agent_type: task.status.value for task in tasks}
        return memo.status.value, agent_statuses


async def _teardown_soak_fixtures(plan_id: str) -> None:
    """Delete the seeded plan (cascades memos + agent tasks) and its request.

    The ``User`` and ``Company`` rows are left in place — they are harmless and
    make repeat runs cheap.
    """
    async with session_scope() as session:
        plan = (
            await session.execute(select(ResearchPlan).where(ResearchPlan.id == plan_id))
        ).scalar_one_or_none()
        if plan is None:
            return
        request_id = plan.request_id
        await session.delete(plan)
        await session.flush()
        request = (
            await session.execute(select(ResearchRequest).where(ResearchRequest.id == request_id))
        ).scalar_one_or_none()
        if request is not None:
            await session.delete(request)
        await session.commit()


def run_soak(runs: int, ticker: str, keep: bool) -> None:
    """Orchestrate the soak: seed, then call the real task N times in a row.

    The Celery task is synchronous and runs its own ``asyncio.run(...)``
    internally, so this function must NOT be inside a running event loop when it
    calls the task. The seeding and reporting coroutines are therefore each
    driven by their own ``asyncio.run(...)`` around the synchronous task call,
    rather than wrapping the whole loop in one long-lived loop — getting that
    wrong would itself raise a loop error and mask the real signal.
    """
    ticker = ticker.upper()
    print("\n=== DEBT-03 event-loop soak ===")
    print(f"  runs={runs}  ticker={ticker}  pid={os.getpid()}")

    plan_id, user_id, memo_ids = asyncio.run(_seed_soak_fixtures(runs, ticker))
    print(f"  seeded: plan={plan_id}  user={user_id}  memos={len(memo_ids)}")

    try:
        for index, memo_id in enumerate(memo_ids, start=1):
            print(f"\n=== Run {index} of {runs}  (memo={memo_id}) ===")
            run_started = datetime.now(UTC)
            # Call the REAL Celery task object synchronously in THIS process.
            # Calling the task runs its body inline here; it is deliberately NOT
            # dispatched to a broker/worker. And the six reset_*() calls plus the
            # asyncio.run(...) boundary are NOT hand-copied into this loop:
            # app/workers/tasks.py owns them, and inlining a copy would silently
            # stop tracking that module — the exact thing under test.
            run_research_task(
                memo_id=memo_id,
                plan_id=plan_id,
                ticker=ticker,
                user_id=user_id,
            )
            memo_status, agent_statuses = asyncio.run(_report_run(memo_id, plan_id, run_started))
            print(f"  memo terminal status: {memo_status}")
            if agent_statuses:
                for agent_type, status in sorted(agent_statuses.items()):
                    print(f"    {agent_type:<24} {status}")
            else:
                print("    (no agent tasks recorded for this run)")
    finally:
        if not keep:
            asyncio.run(_teardown_soak_fixtures(plan_id))
            print(
                "\n  teardown complete: seeded plan/request/memos/agent-tasks "
                "deleted (user + company kept)"
            )
        else:
            print(f"\n  teardown SKIPPED (--keep): plan_id={plan_id} user_id={user_id}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DEBT-03 event-loop soak — N back-to-back research runs, one process.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=_DEFAULT_RUNS,
        help=f"Number of back-to-back research runs (default: {_DEFAULT_RUNS}).",
    )
    parser.add_argument(
        "--ticker",
        default=_DEFAULT_TICKER,
        help=f"Ticker to research on every run (default: {_DEFAULT_TICKER}).",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Skip teardown, leaving the seeded rows in place for inspection.",
    )
    args = parser.parse_args()

    # Install closed-loop traceback detection BEFORE any soak work begins:
    # a logging handler on the root and asyncio loggers catches run-phase
    # occurrences; an sys.unraisablehook wrapper catches GC/teardown ones.
    handler = _PhraseCountingHandler()
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    asyncio_logger = logging.getLogger("asyncio")
    asyncio_logger.addHandler(handler)
    asyncio_logger.setLevel(logging.DEBUG)

    unraisable_hits: list[str] = []
    _install_unraisable_counter(unraisable_hits)
    atexit.register(_atexit_summary, handler, unraisable_hits)

    try:
        run_soak(args.runs, args.ticker, args.keep)
        _emit_verdict(handler, unraisable_hits)
    except SoakFailure as exc:
        print(f"\n=== FAIL ===\n{exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - top-level script boundary
        print(f"\n=== FAIL (unexpected error) ===\n{exc!r}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
