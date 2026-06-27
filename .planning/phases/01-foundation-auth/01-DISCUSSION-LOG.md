# Phase 1: Foundation & Auth - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-27
**Phase:** 1-foundation-auth
**Areas discussed:** SQLAlchemy async vs sync, Project layout, Config / settings, Schema scope for deferred tables

---

## SQLAlchemy async vs sync

| Option | Description | Selected |
|--------|-------------|----------|
| Async — asyncpg + AsyncSession | FastAPI and LangGraph agents are both async. No threadpool overhead. Slightly more complex session management. | ✓ |
| Sync — psycopg2 + Session | Simpler session handling; FastAPI runs sync in threadpool. But LangGraph agents calling DB would need run_in_executor wrappers. | |

**User's choice:** Async — asyncpg + AsyncSession

---

| Option | Description | Selected |
|--------|-------------|----------|
| Dependency injection via get_db() | async def get_db() yields AsyncSession; routes declare Depends(get_db). Easy to override in tests. | ✓ |
| Middleware-attached session | Session attached to request.state in middleware. Harder to mock in tests. | |

**User's choice:** Dependency injection via get_db()

---

| Option | Description | Selected |
|--------|-------------|----------|
| app.dependency_overrides[get_db] | Override get_db in test setup to return a test AsyncSession. No extra infra. | |
| pytest-asyncio fixtures + separate test DB | Separate test PostgreSQL (via docker-compose.test.yml). More realistic. | ✓ |

**User's choice:** pytest-asyncio fixtures + separate test DB

---

| Option | Description | Selected |
|--------|-------------|----------|
| docker-compose.test.yml with dedicated test service | Separate docker-compose.test.yml spins up test-postgres on different port. Simple, no extra deps. | ✓ |
| TestContainers (Python) | testcontainers-python spins up fresh Postgres per test session. No separate compose file, but slower first-run. | |

**User's choice:** docker-compose.test.yml with a dedicated test service

---

## Project layout

| Option | Description | Selected |
|--------|-------------|----------|
| By layer — api/, agents/, services/, models/, db/ | Each directory is a functional layer. Matches CLAUDE.md architecture conventions exactly. | ✓ |
| By feature — auth/, research/, ingestion/ | Each feature owns its routes, models, services. Better for large teams; overkill for walking skeleton. | |

**User's choice:** By layer

---

| Option | Description | Selected |
|--------|-------------|----------|
| Versioned with APIRouter per domain | app/api/v1/auth.py contains all auth routes. One file per domain. | ✓ |
| One file per endpoint | app/api/v1/auth/register.py, login.py, logout.py each export a router. Maximum separation. | |

**User's choice:** Versioned with APIRouter per domain

---

| Option | Description | Selected |
|--------|-------------|----------|
| app/models/ — one file per domain | app/models/auth.py has RegisterRequest, LoginRequest, TokenResponse. Separate from ORM models. | ✓ |
| Alongside routes in app/api/ | Schemas co-located with route file. Conflates API schema with business logic. | |

**User's choice:** app/models/ — one file per domain

---

| Option | Description | Selected |
|--------|-------------|----------|
| app/db/models.py — all models in one file | Single file with all 9 ORM models. Easy for Alembic autogenerate. | ✓ |
| app/db/models/ — one file per model | More granular, 9 small files. Premature for Phase 1. | |

**User's choice:** app/db/models.py — all models in one file

---

## Config / settings

| Option | Description | Selected |
|--------|-------------|----------|
| pydantic-settings BaseSettings | Typed fields, reads from .env, missing vars fail at startup. | ✓ |
| Raw os.getenv() | Simple, no extra dep. No type validation, missing vars fail silently at runtime. | |

**User's choice:** pydantic-settings BaseSettings

---

| Option | Description | Selected |
|--------|-------------|----------|
| Module-level singleton: settings = Settings() | Imported directly. Simple, predictable, zero overhead. | |
| FastAPI dependency: Depends(get_settings) | Allows overriding in tests via dependency_overrides. | ✓ |

**User's choice:** FastAPI dependency: Depends(get_settings)

---

| Option | Description | Selected |
|--------|-------------|----------|
| app/main.py with create_app() factory | Factory returns fresh FastAPI instance. Tests get clean isolated instances. | ✓ |
| app/main.py with module-level app = FastAPI() | Simpler but global shared app instance, harder to test. | |

**User's choice:** app/main.py with create_app() factory

---

## Schema scope for deferred tables

| Option | Description | Selected |
|--------|-------------|----------|
| Full schema now — all columns defined | Define all columns in initial Alembic migration. No ALTER TABLE mid-milestone. | ✓ |
| Stub with id + timestamps only | Tables exist but minimal columns. Breaking migration work in Phase 2. | |

**User's choice:** Full schema now

---

| Option | Description | Selected |
|--------|-------------|----------|
| UUID (server_default=func.gen_random_uuid()) | No ID collision, safe to expose in URLs, no sequential enumeration. | ✓ |
| Integer autoincrement | Simpler, shorter in URLs, but sequential/enumerable. | |

**User's choice:** UUID

---

| Option | Description | Selected |
|--------|-------------|----------|
| Uppercase on write, VARCHAR(20) PK | Stored uppercase always. SQLAlchemy event or service-layer normalization. | ✓ |
| Case-sensitive as received | Ticker stored as provided. Risks 'AAPL' and 'aapl' as distinct entries. | |

**User's choice:** Uppercase on write, VARCHAR(20) PK

---

| Option | Description | Selected |
|--------|-------------|----------|
| Python Enum + VARCHAR with SQLAlchemy Enum type | str enum, VARCHAR column, CHECK constraint. Easy to add values via migration. | ✓ |
| PostgreSQL native CREATE TYPE enum | Stricter at DB level, but ALTER TYPE required to add values — painful for walking skeleton. | |

**User's choice:** Python Enum + VARCHAR with SQLAlchemy Enum type

---

## Claude's Discretion

None — user selected explicit choices for all questions.

## Deferred Ideas

None — discussion stayed within Phase 1 scope.
