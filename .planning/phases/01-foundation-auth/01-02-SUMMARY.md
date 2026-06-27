---
phase: "01-foundation-auth"
plan: "02"
subsystem: "core-scaffold"
status: complete
tags: ["fastapi", "sqlalchemy", "pydantic-settings", "orm", "async"]
dependency_graph:
  requires: ["01-01-PLAN.md"]
  provides:
    - "app.core.config.Settings — typed settings with fail-fast validation"
    - "app.main.create_app — FastAPI application factory"
    - "app.db.base.Base — DeclarativeBase for all ORM models"
    - "app.db.session.get_session — async session dependency"
    - "app.db.models — all 9 ORM classes + 6 status enums"
  affects: ["all subsequent plans (01-03 through 01-08 and every phase after)"]
tech_stack:
  added:
    - "pydantic-settings==2.2.1 — BaseSettings for typed env config"
    - "sqlalchemy[asyncio]==2.0.30 — ORM with asyncpg async engine"
    - "fastapi==0.111.0 — async web framework"
  patterns:
    - "Application factory pattern: create_app() returns fresh FastAPI instance"
    - "Lazy singleton engine: _get_session_factory() creates engine once on first DB call"
    - "Guarded router import: try/except ImportError in create_app() for incremental wiring"
    - "str+Enum pattern: all status enums inherit (str, enum.Enum) for SAEnum compatibility"
key_files:
  created:
    - "app/__init__.py"
    - "app/core/__init__.py"
    - "app/core/config.py"
    - "app/main.py"
    - "app/api/__init__.py"
    - "app/api/v1/__init__.py"
    - "app/agents/__init__.py"
    - "app/services/__init__.py"
    - "app/models/__init__.py"
    - "app/db/__init__.py"
    - "app/db/base.py"
    - "app/db/session.py"
    - "app/db/models.py"
    - "app/graph/__init__.py"
    - "app/ingestion/__init__.py"
    - "app/workers/__init__.py"
  modified: []
decisions:
  - "Lazy engine init in session.py avoids DATABASE_URL requirement at import time — tests that override get_settings() work without env var"
  - "guarded router import (try/except ImportError) in create_app() allows incremental plan build; plan 01-05 populates app/api/v1/__init__.py"
  - "str+enum.Enum inheritance: SAEnum stores VARCHAR, Python gets enum semantics, Alembic can add values without ALTER TYPE"
metrics:
  duration_minutes: 6
  completed_date: "2026-06-27"
  tasks_completed: 2
  files_created: 16
  files_modified: 0
requirements_fulfilled: ["AUTH-01", "AUTH-02", "AUTH-03"]
---

# Phase 01 Plan 02: App Structure, Settings, FastAPI Factory, and ORM Models Summary

**One-liner:** FastAPI application factory with pydantic-settings config, lazy async SQLAlchemy session, and all 9 ORM models using UUID PKs, str+Enum status columns, and soft-delete on ResearchMemo.

## What Was Built

### Task 1: App directory structure, Settings config, and FastAPI factory

Created the full `app/` subpackage hierarchy (11 `__init__.py` markers), the `Settings(BaseSettings)` class in `app/core/config.py`, and the `create_app()` factory in `app/main.py`.

Key design choices:
- `Settings` has no default values for `DATABASE_URL` or `JWT_SECRET_KEY` — missing fields raise `ValidationError` at startup (fail-fast per T-01-02-01)
- `create_app()` uses a guarded router import (`try/except ImportError`) so the empty `app/api/v1/__init__.py` at this stage does not break the factory; plan 01-05 wires the real router
- Module-level `app = create_app()` provides the uvicorn entrypoint

**Commit:** eea0649

### Task 2: SQLAlchemy Base, async session, and all 9 ORM models

Created `app/db/base.py` (DeclarativeBase), `app/db/session.py` (lazy async engine + `get_session` async generator dependency), and `app/db/models.py` (6 enums + 9 ORM classes).

Key design choices:
- Engine is created lazily in `_get_session_factory()` — importing `app.db.session` never fails without `DATABASE_URL`; critical for test fixtures that use `dependency_overrides[get_settings]`
- All tables except `companies` use UUID PKs via PostgreSQL `gen_random_uuid()` (D-12)
- `companies.ticker` is `VARCHAR(20)` PK — uppercase normalisation enforced at service layer (D-13)
- `users.email` has a DB-level UNIQUE constraint to catch race-condition duplicate registrations (T-01-02-02); `IntegrityError` maps to 409 in auth service (plan 01-04)
- `ResearchMemo.deleted_at` implements soft-delete; no hard deletes (T-01-02-04)
- Status enums use `(str, enum.Enum)` so `SAEnum` stores VARCHAR with CHECK constraint; adding new enum values requires only a new migration, not `ALTER TYPE`

**Commit:** 186cb66

## Verification Results

All four plan-level verification assertions passed:

```
python3 -c "from app.db.models import User, Company; print('OK')"         → OK
python3 -c "from app.main import app; print(app.title)"                   → Vantage
python3 -c "from app.db.base import Base; from app.db.models import User; \
            assert 'users' in Base.metadata.tables"                       → OK
python3 -c "from app.db.session import get_session; import inspect; \
            assert inspect.isasyncgenfunction(get_session)"                → OK
```

`Base.metadata.tables` has exactly 9 entries: `users`, `companies`, `documents`, `document_chunks`, `research_requests`, `research_plans`, `research_memos`, `agent_tasks`, `agent_outputs`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Local environment had SQLAlchemy 1.4.54 instead of required 2.0.30**
- **Found during:** Task 2 verification (`from sqlalchemy.orm import DeclarativeBase` → ImportError)
- **Issue:** The plan specifies SQLAlchemy 2.0 APIs (`DeclarativeBase`, `async_sessionmaker`) but the local Python environment had 1.4.54 installed. The project's `requirements/base.txt` already specifies `sqlalchemy[asyncio]==2.0.30`.
- **Fix:** Ran `pip install -r requirements/base.txt` to install the pinned project dependencies. All packages were already verified in the requirements file (not unknown packages). The warnings about `mcp` and `mlflow` transitive conflicts are unrelated to this project.
- **Impact:** None — the code was written correctly for 2.0 throughout; only the local execution environment needed updating.

## Known Stubs

None — all files produce fully functional, importable modules. The `app/api/v1/__init__.py` is intentionally empty at this stage (populated in plan 01-05 with the auth router). The guarded import in `create_app()` handles this correctly.

## Threat Flags

The `/health` endpoint introduced in `app/main.py` is a low-severity surface addition: it exposes liveness without authentication. This is intentional (health checks by load balancers/Docker require unauthenticated access) and was explicitly specified in the plan. No new threat surface beyond what the plan's threat model covers.

## Self-Check: PASSED

Files created:
- app/__init__.py: FOUND
- app/core/config.py: FOUND
- app/main.py: FOUND
- app/db/base.py: FOUND
- app/db/session.py: FOUND
- app/db/models.py: FOUND

Commits verified:
- eea0649 (Task 1): FOUND
- 186cb66 (Task 2): FOUND
