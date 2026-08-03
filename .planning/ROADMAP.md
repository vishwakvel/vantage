# Roadmap: Vantage

## Milestones

- ✅ **v1.0 Walking Skeleton** — Phases 1-4 (shipped 2026-07-03)
- ✅ **v2.0 Full Agent Suite & Observability** — Phases 5-12 (shipped 2026-08-03)

## Phases

<details>
<summary>✅ v1.0 Walking Skeleton (Phases 1-4) — SHIPPED 2026-07-03</summary>

- [x] Phase 1: Foundation & Auth (8/8 plans) — completed 2026-06-27
- [x] Phase 2: Document Ingestion Pipeline (6/6 plans) — completed 2026-06-28
- [x] Phase 3: Research Request & Disambiguation (4/4 plans) — completed 2026-07-01
- [x] Phase 4: Minimal Agent Run (5/5 plans) — completed 2026-07-02

Full detail archived at `.planning/milestones/v1.0-ROADMAP.md`.

</details>

<details>
<summary>✅ v2.0 Full Agent Suite & Observability (Phases 5-12) — SHIPPED 2026-08-03</summary>

- [x] Phase 5: Full Agent Suite & Parallel Fan-Out (10/10 plans) — completed 2026-07-05
- [x] Phase 6: Async Execution & Live Progress (7/7 plans) — completed 2026-07-06
- [x] Phase 7: Contradictions & Memo Polish (5/5 plans) — completed 2026-07-23
- [x] Phase 8: Follow-Up Chat (7/7 plans) — completed 2026-07-26
- [x] Phase 9: Financial Metrics & Anomaly Detection (7/7 plans) — completed 2026-07-27
- [x] Phase 10: Watchlist & Alert Rules (7/7 plans) — completed 2026-07-28
- [x] Phase 11: Alert Evaluation & Notifications (11/11 plans) — completed 2026-07-31
- [x] Phase 12: Observability & Offline Eval (14/14 plans) — completed 2026-08-03

Full detail archived at `.planning/milestones/v2.0-ROADMAP.md`.

</details>

## Progress

| Phase             | Milestone | Plans Complete | Status      | Completed |
| ----------------- | --------- | --------------- | ----------- | ---------- |
| 1. Foundation & Auth | v1.0 | 8/8 | Complete | 2026-06-27 |
| 2. Document Ingestion Pipeline | v1.0 | 6/6 | Complete | 2026-06-28 |
| 3. Research Request & Disambiguation | v1.0 | 4/4 | Complete | 2026-07-01 |
| 4. Minimal Agent Run | v1.0 | 5/5 | Complete | 2026-07-02 |
| 5. Full Agent Suite & Parallel Fan-Out | v2.0 | 10/10 | Complete | 2026-07-05 |
| 6. Async Execution & Live Progress | v2.0 | 7/7 | Complete | 2026-07-06 |
| 7. Contradictions & Memo Polish | v2.0 | 5/5 | Complete | 2026-07-23 |
| 8. Follow-Up Chat | v2.0 | 7/7 | Complete | 2026-07-26 |
| 9. Financial Metrics & Anomaly Detection | v2.0 | 7/7 | Complete | 2026-07-27 |
| 10. Watchlist & Alert Rules | v2.0 | 7/7 | Complete | 2026-07-28 |
| 11. Alert Evaluation & Notifications | v2.0 | 11/11 | Complete | 2026-07-31 |
| 12. Observability & Offline Eval | v2.0 | 14/14 | Complete | 2026-08-03 |

## Backlog

### Phase 999.1: No persisted regression test for cross-event-loop reset_session_factory invariant (BACKLOG)

**Goal:** [Captured for future planning]
**Requirements:** TBD
**Plans:** 14/14 plans complete

Flagged during Phase 6 live verification (2026-07-06). `app/db/session.py::reset_session_factory()` was manually spot-checked live (two successive `reset_session_factory()` + `asyncio.run(session_scope() ...)` cycles against real test-Postgres produced two distinct engine objects with no cross-loop error), but no automated test exercises this specific invariant — only ad hoc verification. Non-blocking; worth a persisted regression test in a future phase.

Plans:

- [x] 12-01-PLAN.md
- [x] 12-02-PLAN.md
- [x] 12-03-PLAN.md
- [x] 12-04-PLAN.md
- [x] 12-05-PLAN.md
- [x] 12-06-PLAN.md
- [x] 12-07-PLAN.md
- [x] 12-08-PLAN.md
- [x] 12-09-PLAN.md
- [x] 12-10-PLAN.md
- [x] 12-11-PLAN.md
- [x] 12-12-PLAN.md
- [x] 12-13-PLAN.md
- [x] 12-14-PLAN.md

- [ ] TBD (promote with /gsd-review-backlog when ready)

### Phase 999.2: ComparableCompanies peer selection is poor for mega-caps (BACKLOG)

**Goal:** [Captured for future planning]
**Requirements:** TBD
**Plans:** 0 plans

Flagged during Phase 6 live verification (2026-07-06). `comparable_companies.py` sources peers from yfinance's industry-key `top_companies` list (D-05). For a mega-cap that dominates a narrow industry bucket (e.g. AAPL at 99.96% market weight in yfinance's "consumer-electronics" industry), the remaining `top_companies` entries are tiny, unrelated tickers (SONO, TBCH, AXIL, FXHO, WTO) instead of real competitors (MSFT, GOOGL, META, AMZN — all classified under different yfinance industries). Confirmed live against the real yfinance API — not a bug, a real limitation of the industry-key approach. Revisit with market-cap-tier matching or a curated override list for large caps in a future phase.

Plans:

- [ ] TBD (promote with /gsd-review-backlog when ready)

### Phase 999.3: Stray "Event loop is closed" tracebacks not attached to any agent failure (BACKLOG)

**Goal:** [Captured for future planning]
**Requirements:** TBD
**Plans:** 0 plans

Flagged during Phase 6 live verification (2026-07-06). After fixing the httpx-client-singleton event-loop bug (reset_news_client/reset_arxiv_client/reset_edgar_client/reset_groq_client, wired into run_research_task), 2 stray `RuntimeError: Event loop is closed` tracebacks still appeared in worker logs across several live runs — but neither was attached to any of that run's actual FAILED agent tasks (all real failures in those runs had clean, complete `groq.RateLimitError` tracebacks with no event-loop error in them). Likely garbage-collection noise from a previous run's now-orphaned httpx client object being finalized at an unpredictable later moment — cosmetic, not functional. Confirmed 0 occurrences under normal single-run conditions. Revisit if it recurs with more live testing once the Groq daily quota resets.

Plans:

- [ ] TBD (promote with /gsd-review-backlog when ready)
