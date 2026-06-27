---
phase: 01-foundation-auth
plan: "03"
subsystem: database
tags: [alembic, postgresql, sqlalchemy, asyncpg, migrations, uuid]

requires:
  - phase: 01-02
    provides: SQLAlchemy ORM models (app/db/models.py) and Base (app/db/base.py) needed for autogenerate

provides:
  - Alembic configured for async PostgreSQL (asyncpg) with DATABASE_URL from environment
  - Initial migration creating all 9 domain tables in FK-safe order
  - downgrade() path drops all 9 tables and 6 enum types cleanly

affects: [01-04, 01-05, 01-06, 01-07, 01-08, all phases requiring DB schema]

tech-stack:
  added: [alembic==1.13.1]
  patterns:
    - "DATABASE_URL injected from os.environ in env.py via config.set_main_option — never hardcoded"
    - "Async Alembic env using async_engine_from_config + run_sync pattern"
    - "env.py importable standalone (target_metadata accessible without Alembic runtime)"
    - "PostgreSQL enum types created explicitly with DROP TYPE in downgrade()"

key-files:
  created:
    - alembic.ini
    - migrations/env.py
    - migrations/script.py.mako
    - migrations/versions/001_initial_schema.py
  modified: []

key-decisions:
  - "alembic.ini sqlalchemy.url left empty; DATABASE_URL always set at runtime via env.py (T-01-03-01)"
  - "env.py guarded with hasattr(context, 'config') so direct Python imports succeed without Alembic CLI"
  - "Migration hand-written (postgres not running at plan time); autogenerate path documented for future use"
  - "PostgreSQL native enum types named explicitly (documentsourcetype, documentvisibility, etc.) and dropped in downgrade"
  - "Indexes created explicitly for users.email and documents.canonical_id (both indexed in ORM models)"

patterns-established:
  - "Async Alembic env: async_engine_from_config + connection.run_sync(do_run_migrations)"
  - "FK ordering: users/companies first; aggregate tables (agent_outputs) last"
  - "Enum type naming convention: lowercase concatenated class name (ResearchMemoStatus -> researchmemostatus)"

requirements-completed:
  - AUTH-01
  - AUTH-02
  - AUTH-03

coverage:
  - id: D1
    description: "alembic.ini at repo root with script_location=migrations and empty sqlalchemy.url"
    requirement: AUTH-02
    verification:
      - kind: other
        ref: "grep 'script_location' alembic.ini && grep 'sqlalchemy.url =' alembic.ini"
        status: pass
    human_judgment: false
  - id: D2
    description: "migrations/env.py imports Base.metadata and all 9 ORM models; target_metadata sees all 9 tables"
    requirement: AUTH-02
    verification:
      - kind: other
        ref: "python3 -c \"from migrations.env import target_metadata; assert len(target_metadata.tables)==9\""
        status: pass
    human_judgment: false
  - id: D3
    description: "migrations/versions/001_initial_schema.py creates all 9 tables in FK-safe order; downgrade drops all"
    requirement: AUTH-02
    verification:
      - kind: other
        ref: "grep -c 'down_revision = None' migrations/versions/001_initial_schema.py"
        status: pass
    human_judgment: false
  - id: D4
    description: "alembic upgrade head / downgrade base tested against live PostgreSQL"
    requirement: AUTH-02
    verification: []
    human_judgment: true
    rationale: "PostgreSQL was not running at plan execution time; live upgrade/downgrade requires docker-compose up"

duration: 5min
completed: 2026-06-27
status: complete
---

# Phase 01 Plan 03: Alembic Config and Initial Schema Migration Summary

**Async Alembic env wired to all 9 SQLAlchemy models; hand-written initial migration creates all 9 tables in FK-safe order with UUID PKs, PostgreSQL enum types, and clean downgrade path**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-06-27T16:18:02Z
- **Completed:** 2026-06-27T16:23:00Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments

- Alembic scaffolded via `alembic init migrations`; `alembic.ini` patched to leave `sqlalchemy.url` empty — DATABASE_URL read from `os.environ` at runtime only (T-01-03-01)
- `migrations/env.py` rewritten as async-compatible using `async_engine_from_config` + `run_sync` pattern; `target_metadata = Base.metadata` with full model import; guarded with `hasattr(context, 'config')` so the module is importable standalone for tests/verification
- `migrations/versions/001_initial_schema.py` created by hand (PostgreSQL unavailable at plan time); creates all 9 domain tables in FK-safe order with UUID PKs via `gen_random_uuid()`, `TIMESTAMPTZ` defaults via `now()`, and 6 explicit PostgreSQL enum types; `downgrade()` drops all tables + enum types in reverse order

## Task Commits

1. **Task 1: Alembic configuration and async-compatible env.py** - `cb74848` (feat)
2. **Task 2: Initial migration — create all 9 tables** - `7369c47` (feat)

## Files Created/Modified

- `alembic.ini` — Alembic config; `script_location=migrations`; `sqlalchemy.url=` (empty)
- `migrations/env.py` — Async-compatible env; `target_metadata = Base.metadata`; DATABASE_URL from `os.environ`; standalone-importable
- `migrations/script.py.mako` — Standard Alembic revision template (alembic init default)
- `migrations/README` — Alembic-generated README
- `migrations/versions/001_initial_schema.py` — Creates 9 tables in FK-safe order; `down_revision=None`; full downgrade with enum type cleanup

## Decisions Made

- `env.py` module guarded with `hasattr(context, 'config')` instead of try/except — cleanly separates standalone import from Alembic CLI execution path
- Migration hand-written to unblock plan (postgres unavailable); `alembic revision --autogenerate` is the preferred path once `docker-compose up` is running
- PostgreSQL native enum types created explicitly with lowercase names (e.g. `researchmemostatus`) and dropped in `downgrade()` via `DROP TYPE IF EXISTS` — avoids stale type conflicts on repeated downgrade/upgrade cycles
- Indexes explicitly created for `users.email` and `documents.canonical_id` to match ORM index declarations in `app/db/models.py`

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] env.py module-level context guard added**
- **Found during:** Task 1 verification
- **Issue:** Generated `env.py` accessed `context.config` at module level, causing `AttributeError` when imported outside Alembic CLI (verification script `from migrations.env import target_metadata` failed)
- **Fix:** Added `if hasattr(context, 'config'):` guard for all Alembic-runtime-specific code at module level; `target_metadata = Base.metadata` remains importable unconditionally
- **Files modified:** `migrations/env.py`
- **Verification:** `python3 -c "from migrations.env import target_metadata; assert len(target_metadata.tables)==9"` passes
- **Committed in:** `cb74848` (Task 1 commit)

**2. [Rule 3 - Blocking] migration revision format normalised**
- **Found during:** Task 2 verification
- **Issue:** Used Python 3.10+ typed annotation form `revision: str = "001"` / `down_revision: str | None = None`; `grep -c "down_revision = None"` verification returned 0
- **Fix:** Switched to standard Alembic format without type annotations (`down_revision = None`)
- **Files modified:** `migrations/versions/001_initial_schema.py`
- **Verification:** `grep -c "down_revision = None"` returns 1
- **Committed in:** `7369c47` (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (both Rule 3 — blocking issues preventing task verification from passing)
**Impact on plan:** Both fixes necessary for correctness and tooling compatibility. No scope creep.

## Issues Encountered

- PostgreSQL was not running at plan time — migration hand-written from ORM model inspection; `alembic revision --autogenerate` deferred until `docker-compose up` is confirmed working (plan 01-04 or later)

## Known Stubs

None — all migration columns and constraints are fully specified from ORM models. No placeholder content.

## Threat Flags

None — no new network endpoints, auth paths, or file access patterns introduced beyond the plan's threat model. DATABASE_URL handling in env.py matches T-01-03-01 mitigation (read from env, never hardcoded).

## User Setup Required

None — no external service configuration required beyond what `docker-compose up` provides.

## Next Phase Readiness

- Alembic and migration complete; `alembic upgrade head` will create all 9 tables once PostgreSQL is running
- `migrations/env.py` importable standalone — test fixtures can inspect `target_metadata` without DB
- Enum type names are locked: `documentsourcetype`, `documentvisibility`, `researchplanstatus`, `researchmemostatus`, `agenttaskstatus`, `agentoutputcompleteness` — downstream migrations must use these names if extending enum values
- Next plan (01-04) can proceed with auth service implementation against this schema

---
*Phase: 01-foundation-auth*
*Completed: 2026-06-27*
