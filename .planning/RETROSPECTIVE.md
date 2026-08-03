# Project Retrospective

*A living document updated after each milestone. Lessons feed forward into future planning.*

## Milestone: v1.0 — Walking Skeleton

**Shipped:** 2026-07-03
**Phases:** 4 | **Plans:** 23 | **Tasks:** 42

### What Was Built
- Foundation: Docker Compose scaffold (Postgres, Redis, ChromaDB, API), JWT auth, Groq rate limiter, EDGAR client, Company entity, section_constants
- Document ingestion: EDGAR auto-fetch, hybrid RAG (dense + BM25 + cross-encoder rerank), canonical_id dedup, private-doc user isolation
- Request intake: free-text → confidence-gated ticker resolution, ClarificationResponse, multi-ticker, private PDF attach
- Agent pipeline: FundamentalAnalysis + Synthesis wired into a 2-node LangGraph, producing a structured, cited ResearchMemo end-to-end

### What Worked
- TDD RED→GREEN discipline across all 23 plans kept the mocked test suite (221 tests) meaningful and fast (~9s full run)
- Day-one architectural boundaries (Groq rate limiter, EDGAR User-Agent, Company entity, section_constants single source of truth) enforced before feature code — no retrofitting needed
- Structured SUMMARY.md per plan made reconstructing the full picture (for both the graphify knowledge graph and this retrospective) fast even without re-reading every diff

### What Was Inefficient
- Phase 2 shipped without ever running `/gsd-verify-work` — its VERIFICATION.md was silently missing, and this went unnoticed until milestone-close readiness checks caught it. Closing the gap required a full live re-verification session that found 5 real bugs.
- The entire mocked test suite gave 221/221 green while the actual EDGAR integration was non-functional against the live API (wrong search parameter, wrong field names, wrong primary-document detection) and the actual `docker-compose up` path was broken three separate ways (dependency pin conflict, container networking, missing curl in the image). None of this was visible without actually running the real stack.
- Fixing the live-verification bugs required a lot of manual protocol reverse-engineering (curling real EDGAR endpoints to discover the actual response schema) that could have been front-loaded during Phase 2's original implementation if a live smoke test had been part of that phase's own verification step.

### Patterns Established
- `_ensure_company_exists()`-style upsert-before-insert guards are now the established pattern for any table with a required FK into a "registry" entity (Company) that isn't guaranteed to pre-exist — worth checking for the same gap before Milestone 2 adds FinancialMetric/Watchlist tables with similar FK shapes.
- Pin client and server versions of any dual-sided dependency (ChromaDB client vs. server image) together, not independently — a client/server version drift caused every real ChromaDB call to fail despite the client version being "correctly pinned" per its own requirements.txt entry.
- For any phase whose test suite mocks 100% of its external dependencies (EDGAR, ChromaDB, real DB session), a real live smoke test before marking the phase verified is not optional — mocked-green does not imply live-green.

### Key Lessons
1. "All tests pass" and "phase is complete" are different claims when every external dependency is mocked — schedule at least one live, real-service smoke test per phase before calling it verified, not just at milestone close.
2. A missing VERIFICATION.md is a real signal, not paperwork — it correlated exactly with the one phase that turned out to have product-critical bugs.
3. When a resource-registry table (Company) is only ever *read* by a resolution/matching service and never *written*, check explicitly whether anything actually persists it — "matches against a seed list" is not the same as "creates a row."
4. `.planning/` is local tooling state for this project, not app code — keep it out of git (`.gitignore`'d) rather than force-adding it; the two are easy to conflate mid-session.

### Cost Observations
- Sessions: 1 extended session covered milestone status check, milestone-close audit, retroactive Phase 2 verification (5 bug fixes), and archival
- Notable: the live-verification detour (finding and fixing 5 bugs) took roughly as long as the original milestone-status question — worth treating "is it actually verified" as a standing question before any milestone-close, not an afterthought

---

## Milestone: v2.0 — Full Agent Suite & Observability

**Shipped:** 2026-08-03
**Phases:** 8 | **Plans:** 68 | **Tasks:** 142

### What Was Built
- Full 7-agent suite in true 5-way parallel fan-out (SentimentNLP, RiskAssessment, MacroSector, ComparableCompanies alongside FundamentalAnalysis) with graceful PARTIAL degradation
- Async Celery execution + live WebSocket progress panel; a user can navigate away and return to a completed memo
- Structured, severity-rated Contradictions panel and at-a-glance COMPLETE/PARTIAL status
- Persistent, resumable follow-up chat grounded strictly on memo text (no RAG re-query), with coverage-exceeded flagging
- Structured FinancialMetric storage + isolation-forest anomaly detection surfaced in the fundamentals section
- Personal watchlist with NEW_FILING/PRICE_MOVE/SCHEDULED alert rules, scheduled evaluation, in-app notifications, and alert history
- Full observability: LangSmith traces on every agent call, per-agent cost aggregated to a per-memo total shown in the memo, daily RAGAS offline retrieval-quality eval

### What Worked
- The D-01 env-gated live-smoke-test pattern (established Phase 5, `tests/live/`, opt-in via `RUN_LIVE_TESTS=1`) caught real bugs on first use in multiple phases (arXiv's `http://`→`https://` permanent redirect, GroqResult token-contract gaps) without polluting the default hermetic test run
- Real-PostgreSQL integration tests (not mocks) caught genuine production bugs mocked suites couldn't: `session.rollback()` breaking per-rule isolation and a stale identity-map read-your-own-writes bug, both in Phase 11
- End-of-phase human-verify checkpoints against a real browser + real Groq calls caught what fully-passing automated suites missed every single time: a CSS contrast bug (Phase 10), coverage-flag UX (Phase 8), real memo-cost rendering (Phase 12)
- The per-agent `session_scope()` + `reset_*()` singleton-lifecycle pattern (Phase 5-6) scaled cleanly to true 5-way parallel fan-out with zero cross-agent DB session collisions once established

### What Was Inefficient
- `.planning/` being gitignored — a deliberate v1.0 decision (see v1.0 Key Lesson 4) to avoid conflating tooling state with app code — collided with worktree-isolated parallel execution: SUMMARY.md files for Phase 7 (07-02, 07-05) and Phase 8 (08-01/03/04/06) were silently destroyed on worktree cleanup, each requiring reconstruction from git history + VERIFICATION.md, and this milestone's automated readiness check still flagged Phase 8 as unverified as a result
- `fred_client.py`'s module-level singleton `httpx.AsyncClient` (introduced Phase 5) never received the same `reset_*()` fix Phase 6 applied to the other 4 similar clients (EDGAR/News/arXiv/Groq) — same bug class, left latent, only surfaced during Phase 12 review rather than being caught by the original fix's sibling-audit
- Groq's real per-account TPD/TPM limits (100k TPD, 12k TPM on rapid re-runs) weren't documented as a known operating constraint until they were independently rediscovered during live verification more than once (Phase 6, Phase 12)

### Patterns Established
- D-01 env-gated live smoke test per new external service client (`tests/live/`, `RUN_LIVE_TESTS=1`) — now the default pattern for any new client, not just a Phase 5 special case
- Per-agent `session_scope()` + `reset_*()` lifecycle for every module-level async client that crosses a Celery `asyncio.run()` boundary
- Fenced-JSON-after-narrative single-Groq-call pattern (never-raise parse/repair/validate, degrade to a safe default) — proven twice: Contradictions (Phase 7) and chat coverage flag (Phase 8)
- Throwaway QA harness (temporary URL-param hook + disposable test account + `browse` skill, fully reverted before merge) for human-verify checkpoints that need real rendering without spending real Groq quota on a fresh pipeline run

### Key Lessons
1. A blanket ".planning/ is gitignored" policy needs an explicit exception for durable artifacts (SUMMARY.md, VERIFICATION.md) once worktree-isolated parallel execution enters the picture — v1.0's Key Lesson 4 was correct for single-worktree work and incomplete for v2.0's wave-based parallel execution model. Unresolved going into v3.
2. Fixing one instance of a client-lifecycle bug class is not the same as auditing all siblings for it — `fred_client`'s un-reset singleton is the direct result of Phase 6 fixing 4 of 5 similar clients and not circling back to the 5th once it stopped blocking anything visible.
3. External rate limits (Groq TPD/TPM) are best discovered once, documented as an operating constraint, and referenced going forward — rediscovering the same limit as a fresh "why is this failing" investigation in a later phase wastes real verification time.
4. Live verification found at least one real bug in every phase of this milestone despite fully-passing mocked test suites (Phase 5: 1, Phase 6: 8, Phase 10: 1, Phase 11: 2) — the v1.0 practice of never calling a phase verified without a live pass against real dependencies continued paying for itself throughout v2.0.

### Cost Observations
- Model mix / per-session token cost: not tracked this milestone — worth instrumenting before v3 if cost visibility becomes a priority
- Sessions: 8 phases across 29 days (2026-07-05 → 2026-08-03), including at least 5 executor-retry recoveries from provider session-quota interruptions (Phase 10, Phase 11 ×2, Phase 12 ×2) — atomic commit-per-task discipline meant zero commits lost in any of them
- Full suite grew from 221 tests (v1.0) to 687 passing / 8 skipped (v2.0), alongside ~31,700 LOC Python + ~2,200 LOC TypeScript

---

## Cross-Milestone Trends

### Process Evolution

| Milestone | Sessions | Phases | Key Change |
|-----------|----------|--------|------------|
| v1.0 | 1 (this retrospective's scope) | 4 | Established live-verification-before-close as a required gate, not optional |
| v2.0 | 8 phases, 29 days | 8 | Adopted wave-based parallel executor worktrees; generalized the D-01 live-smoke-test pattern to every new external client |

### Cumulative Quality

| Milestone | Tests | Coverage | Zero-Dep Additions |
|-----------|-------|----------|---------------------|
| v1.0 | 221 | Not measured (no coverage tool configured yet) | 0 |
| v2.0 | 687 passed, 8 skipped | Not measured | 0 (yfinance, scikit-learn, json-repair, langsmith, ragas, rapidfuzz all went through a human supply-chain legitimacy checkpoint before install) |

### Top Lessons (Verified Across Milestones)

1. Mocked-green test suites do not prove live-green behavior for any phase that mocks 100% of its external dependencies — carried forward from v1.0's Phase 2 gap and reconfirmed in every phase of v2.0.
2. A gitignored `.planning/` is safe for single-worktree work but destroys durable plan artifacts (SUMMARY.md) under worktree-isolated parallel execution — this recurred twice in v2.0 (Phase 7, Phase 8) and remains unresolved going into v3.
