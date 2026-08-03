---
gsd_state_version: 1.0
milestone: v2.0
milestone_name: Full Agent Suite & Observability
current_phase: 0
status: Awaiting next milestone
stopped_at: Phase 12 context gathered
last_updated: "2026-08-03T15:34:26.342Z"
last_activity: 2026-08-03
last_activity_desc: Milestone v2.0 completed and archived
progress:
  total_phases: 8
  completed_phases: 7
  total_plans: 68
  completed_plans: 64
  percent: 88
current_phase_name: BACKLOG
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-07-28)

**Core value:** Given a ticker or investment thesis, produce a fully cited ResearchMemo with explicit Contradictions — in minutes, not hours.
**Current focus:** Phase 12 — observability-offline-eval

## Current Position

Phase: Milestone v2.0 complete
Plan: —
Status: Awaiting next milestone
Last activity: 2026-08-03 — Milestone v2.0 completed and archived

## Performance Metrics

**Velocity:**

- Total plans completed: 85
- Average duration (Phase 10): ~19 min/plan
- Total execution time (Phase 10): ~2.2 hours (7 plans, 1 executor retry after a session-quota interruption)

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01 | 8 | - | - |
| 03 | 4 | - | - |
| 04 | 5 | - | - |
| 05 | 10 | - | - |
| 06 | 7 | - | - |
| 07 | 5 | - | - |
| 08 | 7 | - | - |
| 09 | 7 | - | - |
| 10 | 7 | 132 min | ~19 min |
| 11 | 11 | - | - |
| 12 | 14 | - | - |

**Recent Trend:**

- Last 5 plans: 10-03 (16m), 10-04 (25m), 10-05 (20m, retried once), 10-06 (12m), 10-07 (25m)
- Trend: stable, no cross-plan regressions across 4 waves

*Updated after each plan completion*

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- Day-one: Groq rate limiter is non-negotiable — no direct Groq calls anywhere; CI enforced. Parallel agent fan-out (Phase 5) will stress this hardest.
- Day-one: All external API calls live in app/services/ only — never inline in agents (applies to the 4 new agents in Phase 5).
- Day-one: section_constants.py is the single source of truth for all section string literals.
- [Phase 4] Declarative routing only (plain add_edge, no LLM routing) — carry into Phase 5's parallel fan-out design.
- [Phase 4] _compute_memo_status is the locked D-02 degradation rule — Phase 5 fan-in (AGENT-06) extends it to all agents; Phase 7 Contradictions build on the same Synthesis-owns-status boundary.
- [v1.0 lesson] Mocked tests alone missed 5 product-critical bugs in Phase 2 — run a live smoke test against real deps (EDGAR/ChromaDB/Postgres/Groq) before calling a v2.0 phase verified.
- [PROJECT.md ⚠️] Company-row upsert gap (FK violation on new tickers) was invisible to mocked tests — audited for FinancialMetric (Phase 9, held) and Watchlist (Phase 10, held: `ensure_company_exists` called before insert, verified live against real Postgres with a brand-new ticker).
- [Phase 6] Live verification of a fully-passing (307-test) phase found 8 real bugs the mocked suite couldn't see (CORS, EDGAR param, exception logging, chunker mistagging, cross-user retrieval scoping, httpx client event-loop lifecycle, and a hardcoded-variable-name reason bug in Synthesis) — reinforces the Phase 2 lesson at higher stakes. Full detail: vantage-notes/Vantage Build Log.md.
- [Phase 6] Every module-level singleton wrapping a persistent async client (DB engine, EDGAR/news/arXiv/Groq httpx clients) needs a `reset_*()` called before each Celery task's `asyncio.run(...)` — carry this pattern forward for any new external client added in Phase 9+ (FinancialMetric sources, etc.).
- [Phase 10] Live UAT surfaced a real bug the full automated suite (489 tests, clean `tsc`/build) couldn't see: the shared `.input` CSS class was invisible inside any `.panel` container (transparent border, identical background) — CSS contrast defects don't show up in type-checks or test assertions. Fixed app-wide in one commit. Worth a quick visual pass on any new form-in-panel UI in Phase 11+.
- [Phase 10] `app/services/watchlist_service.py` and its rule-config validation matrix are the pattern for Phase 11's alert *evaluation* logic — Phase 11 reads `AlertRule.config`/`rule_type` the same way `create_alert_rule` validates it.

### Pending Todos

None yet.

### Blockers/Concerns

- ⚠️ [Phase 6] Groq daily token quota (100k TPD) exhausted from this session's extensive live-run verification — expect `groq.RateLimitError` on research runs until it resets. External account limit, not an application issue.
- ⚠️ [Phase 6, backlog 999.1] No persisted regression test for the cross-event-loop `reset_session_factory` invariant — only ad hoc live verification exists.
- ⚠️ [Phase 6, backlog 999.2] ComparableCompanies' yfinance-industry-key peer selection produces poor matches for mega-caps (e.g. AAPL → SONO/TBCH/AXIL instead of MSFT/GOOGL). Confirmed real API limitation, not a bug.
- ⚠️ [Phase 6, backlog 999.3] 2 stray "Event loop is closed" tracebacks appeared, not attached to any actual agent failure — likely GC noise from orphaned httpx clients. Non-blocking; revisit if it recurs.

## Deferred Items

Items acknowledged and deferred at milestone close on 2026-08-03:

| Category | Item | Status |
|----------|------|--------|
| verification_override | Phase 8 (Follow-Up Chat) SUMMARY.md files for plans 08-01/03/04/06 lost during worktree merge (`.planning/` is gitignored) — automated readiness check reports `phase_complete: false`. Implementation itself is verified: `08-VERIFICATION.md` (2026-07-26, status passed, 4/4 criteria) covers exactly these plans with file:line evidence, and git history has matching feat/test/docs commits for each. Treated as a documentation-artifact loss, not an implementation gap. | Acknowledged, proceeding |

Items acknowledged and carried forward from previous milestone close (now mapped into the v2.0 roadmap):

| Category | Item | Status | Deferred At |
|----------|------|--------|-------------|
| Agent suite | 5 remaining agents + true parallel fan-out (AGENT-01..06) | Mapped → Phase 5 | v1.0 scope |
| Execution | WebSocket live progress panel (EXEC-01) | Mapped → Phase 6 | v1.0 scope |
| Execution | Async Celery research tasks (EXEC-05) | Mapped → Phase 6 | v1.0 scope |
| Execution | Failed/missing sections marked with reason (EXEC-04) | Mapped → Phase 5 | v1.0 scope |
| Memo | Contradictions panel, PARTIAL status (MEMO-04, 05) | Mapped → Phase 7 | v1.0 scope |
| Chat | Follow-up session layer (CHAT-01..04) | Mapped → Phase 8 | v1.0 scope |
| Metrics | Financial metrics + anomaly detection (METRIC-01..03) | Mapped → Phase 9 | v1.0 scope |
| Watchlist | Watchlist + alert rules (WATCH-01..05, 08) | Mapped → Phase 10 | v1.0 scope |
| Watchlist | Alert evaluation + notifications (WATCH-06, 07, 09) | Mapped → Phase 11 | v1.0 scope |
| Observability | LangSmith traces, cost breakdown, memo cost display, RAGAS eval (OBS-01..03, MEMO-06) | Mapped → Phase 12 | v1.0 scope |

## Session Continuity

Last session: 2026-08-01T13:45:31.984Z
Stopped at: Phase 12 context gathered
Resume file: .planning/phases/12-observability-offline-eval/12-CONTEXT.md

## Operator Next Steps

- Start the next milestone with /gsd-new-milestone
