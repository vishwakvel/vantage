---
phase: 01-foundation-auth
plan: "06"
subsystem: infra
tags: [groq, rate-limiter, token-bucket, edgar, httpx, sec-edgar, section-constants]

requires:
  - phase: 01-02
    provides: app/ directory structure, SQLAlchemy ORM models, services/ package created

provides:
  - AsyncTokenBucketRateLimiter class with async acquire() that blocks (awaits) at 0 tokens
  - groq_rate_limiter module-level singleton (6000 token capacity, 100 tokens/s refill)
  - call_groq() Phase 1 stub that raises NotImplementedError
  - EDGARClient with User-Agent enforced at httpx.AsyncClient level on every request
  - EDGAR_USER_AGENT constant ('Vantage/1.0 vishwak.vel@gmail.com') and EDGAR_BASE_URL
  - edgar_client module-level singleton
  - section_constants.py with 17 non-empty string constants (filing, news, memo sections)

affects:
  - 01-07 (API routes — no direct dependency but needs section_constants for Phase 2+)
  - 01-08 (import guard test — verifies groq_rate_limiter is single entry point for Groq)
  - Phase 2 ingestion pipeline (uses section_constants for Chunk.section values)
  - Phase 4 agents (must import groq via groq_rate_limiter; import guard enforces boundary)

tech-stack:
  added:
    - httpx==0.27.0 (already in requirements/base.txt — used for EDGARClient)
    - ruff + black (installed during execution for lint enforcement)
  patterns:
    - "Token-bucket rate limiter: async acquire() blocks at 0 tokens — never drops, never raises"
    - "Module-level singleton pattern: groq_rate_limiter and edgar_client are single import points"
    - "httpx.AsyncClient default headers: User-Agent set once at client init, enforced on every request"
    - "Section constants module: all section string literals live in section_constants.py only"
    - "TDD RED/GREEN: test file committed first (ImportError = RED), implementation added for GREEN"

key-files:
  created:
    - app/services/groq_client.py
    - app/services/edgar_client.py
    - app/ingestion/section_constants.py
    - tests/services/test_groq_client.py
    - tests/services/test_edgar_client.py
  modified: []

key-decisions:
  - "groq_rate_limiter is the ONLY sanctioned entry point for Groq token consumption — plan 01-08 import guard CI test enforces this boundary"
  - "User-Agent header set at httpx.AsyncClient default headers level — impossible to omit on any request through EDGARClient"
  - "call_groq() raises NotImplementedError with 'Phase 1' message — prevents accidental real LLM calls in Phase 1 tests"
  - "section_constants.py uses flat module-level string assignments (no class, no enum) — simplest possible import pattern"

patterns-established:
  - "Rate limiter singleton: import groq_rate_limiter from app.services.groq_client — never instantiate AsyncTokenBucketRateLimiter directly in agents"
  - "EDGAR client singleton: import edgar_client from app.services.edgar_client — never create EDGARClient instances outside services/"
  - "Section constants: from app.ingestion.section_constants import SECTION_* — never use inline string literals for section names"

requirements-completed:
  - AUTH-01
  - AUTH-02
  - AUTH-03

coverage:
  - id: D1
    description: "AsyncTokenBucketRateLimiter: async acquire() blocks (awaits) at 0 tokens; never raises or drops; groq_rate_limiter singleton with capacity=6000, refill_rate=100"
    requirement: AUTH-01
    verification:
      - kind: unit
        ref: "tests/services/test_groq_client.py#test_acquire_blocks_when_tokens_exhausted"
        status: pass
      - kind: unit
        ref: "tests/services/test_groq_client.py#test_acquire_never_raises_on_empty_bucket"
        status: pass
      - kind: unit
        ref: "tests/services/test_groq_client.py#test_acquire_concurrent_callers_all_complete"
        status: pass
      - kind: unit
        ref: "tests/services/test_groq_client.py#test_groq_rate_limiter_default_capacity"
        status: pass
    human_judgment: false
  - id: D2
    description: "call_groq() Phase 1 stub raises NotImplementedError with 'Phase 1' message — no real Groq calls in Phase 1"
    requirement: AUTH-01
    verification:
      - kind: unit
        ref: "tests/services/test_groq_client.py#test_call_groq_raises_not_implemented"
        status: pass
      - kind: unit
        ref: "tests/services/test_groq_client.py#test_call_groq_raises_for_any_prompt"
        status: pass
    human_judgment: false
  - id: D3
    description: "EDGARClient: User-Agent header 'Vantage/1.0 vishwak.vel@gmail.com' enforced on every outbound request via httpx.AsyncClient default headers"
    requirement: AUTH-02
    verification:
      - kind: unit
        ref: "tests/services/test_edgar_client.py#test_get_sends_user_agent_header"
        status: pass
      - kind: unit
        ref: "tests/services/test_edgar_client.py#test_get_always_includes_user_agent_even_with_extra_headers"
        status: pass
      - kind: unit
        ref: "tests/services/test_edgar_client.py#test_edgar_user_agent_exact_value"
        status: pass
    human_judgment: false
  - id: D4
    description: "EDGARClient async context manager (__aenter__/__aexit__) and edgar_client module-level singleton"
    requirement: AUTH-02
    verification:
      - kind: unit
        ref: "tests/services/test_edgar_client.py#test_async_context_manager_returns_client"
        status: pass
      - kind: unit
        ref: "tests/services/test_edgar_client.py#test_async_context_manager_closes_on_exit"
        status: pass
      - kind: unit
        ref: "tests/services/test_edgar_client.py#test_edgar_client_singleton_exists"
        status: pass
    human_judgment: false
  - id: D5
    description: "section_constants.py: 17 non-empty string constants covering SEC filing, news/article, and research memo sections; importable by name"
    requirement: AUTH-03
    verification:
      - kind: unit
        ref: "tests/services/test_edgar_client.py#test_section_constants_all_non_empty_strings"
        status: pass
      - kind: unit
        ref: "tests/services/test_edgar_client.py#test_section_constants_minimum_count"
        status: pass
      - kind: unit
        ref: "tests/services/test_edgar_client.py#test_section_constants_required_imports"
        status: pass
      - kind: unit
        ref: "tests/services/test_edgar_client.py#test_section_constants_edgar_sections"
        status: pass
      - kind: unit
        ref: "tests/services/test_edgar_client.py#test_section_constants_memo_sections"
        status: pass
    human_judgment: false

duration: 5min
completed: "2026-06-27"
status: complete
---

# Phase 01 Plan 06: Day-one Constraints — Groq Rate Limiter, EDGAR Client, Section Constants Summary

**Async token-bucket rate limiter (6000 tok/min, blocks-never-drops) and EDGAR client (httpx User-Agent enforced at transport level) and 17-constant section_constants module — all three day-one architectural boundaries established before any feature code**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-06-27T16:41:42Z
- **Completed:** 2026-06-27T16:47:00Z
- **Tasks:** 2
- **Files modified:** 5 (3 created implementation + 2 created tests)

## Accomplishments

- `AsyncTokenBucketRateLimiter`: async token-bucket with `acquire()` that awaits at 0 tokens — callers block, never dropped, never raise; `groq_rate_limiter` singleton is the only sanctioned Groq entry point
- `EDGARClient`: httpx.AsyncClient with `User-Agent: Vantage/1.0 vishwak.vel@gmail.com` baked into default headers — impossible to omit on any request through this client; supports async context manager
- `section_constants.py`: 17 non-empty string constants (filing: risk_factors, mda, business, financials, notes, cover; news: headline, body, abstract; memo: fundamentals, synthesis, contradictions, risks, macro, comparables, sentiment) — single source of truth
- 23 unit tests (10 groq + 13 edgar/section_constants), all passing; ruff + black clean

## Task Commits

Each task was committed atomically in TDD RED/GREEN sequence:

1. **Task 1 RED: Groq rate limiter tests** - `ae2eedd` (test)
2. **Task 1 GREEN: Groq rate limiter implementation** - `3c0e16e` (feat)
3. **Task 2 RED: EDGAR client + section_constants tests** - `e8568ae` (test)
4. **Task 2 GREEN: EDGAR client + section_constants implementation** - `bc79f20` (feat)

## Files Created/Modified

- `app/services/groq_client.py` — `AsyncTokenBucketRateLimiter` class, `groq_rate_limiter` singleton (6000 capacity, 100 tokens/s), `call_groq()` Phase 1 stub raising NotImplementedError
- `app/services/edgar_client.py` — `EDGAR_USER_AGENT` constant, `EDGAR_BASE_URL`, `EDGARClient` with `.get()` + async context manager, `edgar_client` singleton
- `app/ingestion/section_constants.py` — 17 module-level string constants across three groups (filing, news, memo)
- `tests/services/test_groq_client.py` — 10 unit tests for rate limiter and stub
- `tests/services/test_edgar_client.py` — 13 unit tests for EDGAR client and section_constants

## Decisions Made

- Token-bucket uses `asyncio.Lock` to serialize token accounting — concurrent callers serialize correctly without race conditions on `_tokens`
- `call_groq()` acquires rate limit budget BEFORE raising NotImplementedError — establishes the correct Phase 4 call pattern (rate limit check precedes API call)
- Section constants use flat module-level assignments (not Enum, not class) — simplest possible import pattern: `from app.ingestion.section_constants import SECTION_RISK_FACTORS`
- Tests use `httpx.MockTransport` (not `unittest.mock.patch`) to verify User-Agent — captures actual request headers as seen by the transport layer

## Deviations from Plan

None — plan executed exactly as written.

The plan's `action` blocks were precise; no architectural changes, missing dependencies, or blocking issues were encountered.

## Issues Encountered

- **ruff + black not installed**: Discovered during post-implementation lint check. Installed via `pip install ruff black` (Rule 3 auto-fix — blocked lint verification). Both pass clean on all new files.
- **Test async marker**: Existing tests use `@pytest.mark.anyio` (from `anyio` package) not `@pytest.mark.asyncio` (from `pytest-asyncio`). Updated test file to match project convention before committing RED.

## Known Stubs

- `call_groq()` in `app/services/groq_client.py`: intentional Phase 1 stub — always raises `NotImplementedError("Groq API calls not implemented in Phase 1")`. Real Groq integration deferred to Phase 4. This is by design per plan prohibitions.
- `app/services/edgar_client.py`: `edgar_client` singleton initialises but never makes real HTTP calls in Phase 1 tests (all test-mocked). Real EDGAR API calls begin in Phase 2.

## Threat Surface Scan

No new network endpoints, auth paths, or schema changes introduced. Both `edgar_client.py` and `groq_client.py` are stubs — no real outbound traffic in Phase 1. Threat model threats T-01-06-01 (import guard) and T-01-06-02 (User-Agent) are mitigated as planned; T-01-06-03 (DoS via rate limiter) is accepted per plan.

## Next Phase Readiness

- Plan 01-07 (API routes): section_constants available for Phase 2+ ingestion references; groq_rate_limiter and edgar_client singletons importable
- Plan 01-08 (import guard): can now write the test that asserts no module in `app/agents/` or `app/graph/` imports `groq` directly; `groq_rate_limiter` is the single sanctioned import point

## Self-Check: PASSED

All files exist on disk:
- FOUND: app/services/groq_client.py
- FOUND: app/services/edgar_client.py
- FOUND: app/ingestion/section_constants.py
- FOUND: tests/services/test_groq_client.py
- FOUND: tests/services/test_edgar_client.py

All task commits exist:
- FOUND: ae2eedd (test(01-06): add failing tests for Groq rate limiter RED)
- FOUND: 3c0e16e (feat(01-06): implement Groq async token-bucket rate limiter GREEN)
- FOUND: e8568ae (test(01-06): add failing tests for EDGAR client and section_constants RED)
- FOUND: bc79f20 (feat(01-06): implement EDGAR client User-Agent enforced and section_constants GREEN)

---
*Phase: 01-foundation-auth*
*Completed: 2026-06-27*
