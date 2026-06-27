# Phase 1: Foundation & Auth - Context

**Gathered:** 2026-06-27
**Status:** Ready for planning

<domain>
## Phase Boundary

Create the project skeleton from a completely greenfield repo: Docker Compose with all four services healthy, full v1.0 SQLAlchemy schema via Alembic migrations, JWT auth (register/login/logout with Redis blocklist), and day-one service stubs (Groq rate limiter, EDGAR HTTP client, section_constants). Dev tooling configured and all linters passing. No actual Groq or EDGAR API calls in this phase.

</domain>

<spec_lock>
## Requirements (locked via SPEC.md)

**9 requirements are locked.** See `01-SPEC.md` for full requirements, boundaries, and acceptance criteria.

Downstream agents MUST read `01-SPEC.md` before planning or implementing. Requirements are not duplicated here.

**In scope (from SPEC.md):**
- `docker-compose.yml` with FastAPI, PostgreSQL 16, Redis 7, ChromaDB — all with health checks and `depends_on: condition: service_healthy` ordering
- SQLAlchemy ORM models for all 9 v1.0 domain tables (full schema, not stubs)
- Alembic configuration and initial migration (upgrade + downgrade)
- `app/api/v1/auth/` — register, login, logout endpoints
- JWT generation and validation; 24h access token; no refresh token
- Redis-based JWT blocklist in `app/services/auth_service.py`
- `app/services/groq_client.py` — async token-bucket rate limiter stub (no actual Groq calls)
- `app/services/edgar_client.py` — HTTP client stub with User-Agent enforcement
- `app/ingestion/section_constants.py` — initial section name constants
- `app/db/` — SQLAlchemy base, session factory, Alembic env
- `pyproject.toml`, `requirements/base.txt`, `requirements/dev.txt`, pytest config
- Import guard test for Groq boundary enforcement
- Unit and integration tests for all auth endpoints and service-layer constraints

**Out of scope (from SPEC.md):**
- Actual Groq API calls — rate limiter is a stub in Phase 1
- Actual EDGAR API calls — client is a stub in Phase 1
- Any ingestion logic — Phase 2
- Research request/disambiguation endpoints — Phase 3
- Agent execution/ResearchMemo — Phase 4
- WebSocket, Celery, async task queue — Milestone 2
- Refresh token flow — 24h access token sufficient for walking skeleton
- Frontend — curl/Postman sufficient to verify Phase 1
- RBAC / multi-role authorization — single user role only

</spec_lock>

<decisions>
## Implementation Decisions

### Database Layer

- **D-01:** Async SQLAlchemy — `asyncpg` driver + `AsyncSession` from `sqlalchemy.ext.asyncio`. All DB interactions are async-native; no threadpool wrappers needed for LangGraph agents (which are async coroutines).
- **D-02:** Session injection via `Depends(get_session)` — `async def get_session()` yields an `AsyncSession`. Routes declare `session: AsyncSession = Depends(get_session)`. Service functions accept session as a parameter.
- **D-03:** Test DB via `docker-compose.test.yml` — a dedicated `test-postgres` service on a different port (e.g., 5433). Tests run with `DATABASE_URL` pointing at it. `pytest-asyncio` fixtures manage session lifecycle per test. This is the test DB isolation strategy — not in-memory SQLite (to stay realistic with PostgreSQL-specific types).

### Project Layout

- **D-04:** By-layer directory structure under `app/`:
  ```
  app/
    api/v1/        # FastAPI routers (one file per domain)
    agents/        # LangGraph agent nodes
    services/      # External API clients only
    models/        # Pydantic request/response schemas
    db/            # SQLAlchemy ORM models + session + Alembic env
    ingestion/     # section_constants.py + (later) chunking/retrieval
    graph/         # LangGraph state and graph construction
    workers/       # Celery tasks (Milestone 2)
    core/          # config.py, security.py, dependencies.py
  ```
- **D-05:** FastAPI routers — one `APIRouter` per domain in `app/api/v1/`. Auth routes all in `app/api/v1/auth.py`. Router files imported into `app/api/v1/__init__.py` which is mounted at `/api/v1` in `create_app()`.
- **D-06:** Pydantic schemas in `app/models/` — one file per domain: `app/models/auth.py` (RegisterRequest, LoginRequest, TokenResponse, UserOut). Separate from SQLAlchemy ORM models.
- **D-07:** SQLAlchemy ORM models in `app/db/models.py` — single file with all 9 ORM classes. Importable by Alembic autogenerate via `target_metadata`. Split into per-model files only when file grows beyond ~300 lines in Phase 2+.

### Config / Settings

- **D-08:** `pydantic-settings` `BaseSettings` in `app/core/config.py`. Typed fields for all environment variables; reads from `.env` file + environment. Missing required vars fail at startup (not at first use).
- **D-09:** Settings injected via `Depends(get_settings)` — allows `app.dependency_overrides[get_settings]` in tests to inject test-specific config (test DB URL, fake JWT secret, etc.) without environment variable pollution.
- **D-10:** `create_app()` factory function in `app/main.py` — returns a fresh `FastAPI` instance. `app = create_app()` at module level for uvicorn. Tests import `create_app()` to get clean, isolated instances.

### Schema Design

- **D-11:** Full schema in Phase 1 — all columns defined for all 9 tables in the initial Alembic migration. No ALTER TABLE mid-milestone. Schema derived from `CONTEXT.md` domain glossary.
- **D-12:** UUID primary keys on all tables — `Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())`. No sequential integer IDs.
- **D-13:** Tickers stored uppercase — `companies.ticker` is `VARCHAR(20)` PK. Service layer normalizes to `.upper()` on insert. Prevents 'AAPL' vs 'aapl' collision.
- **D-14:** Status columns as Python Enum + SQLAlchemy `Enum` type — e.g., `class ResearchMemoStatus(str, Enum): PENDING="PENDING"`. Column declared as `Column(Enum(ResearchMemoStatus))`. Stores as VARCHAR with CHECK constraint. Easy to add new values via migration without ALTER TYPE.

### Auth Implementation

- **D-15:** Password hashing via `passlib[bcrypt]` — `CryptContext(schemes=["bcrypt"])` in `app/core/security.py`. Hash on register, verify on login.
- **D-16:** JWT via `python-jose[cryptography]` — HS256 algorithm. Secret from `settings.JWT_SECRET_KEY`. Token payload: `{"sub": str(user.id), "exp": ...}`. `get_current_user` dependency in `app/core/dependencies.py` decodes and validates JWT, checks Redis blocklist.
- **D-17:** Redis blocklist — `SET revoked:{jti} "" EX {remaining_ttl}` on logout. `get_current_user` checks `EXISTS revoked:{jti}` before accepting token. JTI is `jti` claim in JWT payload (UUID, unique per token).
- **D-18:** On Redis failure during logout — raise `ServiceUnavailableError` mapped to 503. Never return 200 if the token cannot be blocklisted.

### Testing

- **D-19:** Test structure mirrors `app/` structure — `tests/api/`, `tests/services/`, `tests/db/`. pytest-asyncio async fixtures. `conftest.py` at `tests/` root provides: `app` (from `create_app()`), `client` (httpx `AsyncClient`), `session` (test `AsyncSession`), `settings_override` (via `dependency_overrides`).
- **D-20:** Import guard test at `tests/test_boundaries.py` — imports all modules in `app/agents/` and `app/graph/` and asserts none have `groq` in `sys.modules` or import graph.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements and Acceptance Criteria
- `.planning/phases/01-foundation-auth/01-SPEC.md` — Locked requirements, boundaries, acceptance criteria, edge coverage, prohibitions. MUST read before planning.

### Project Architecture
- `CLAUDE.md` — Hard rules: `app/services/` boundary, Groq rate limiter enforcement, EDGAR User-Agent, section_constants location, no direct secrets, feature branch only.
- `CONTEXT.md` — Domain glossary: all 9 entity definitions (fields, relationships, status lifecycle) used to derive the full Phase 1 schema.
- `.planning/PROJECT.md` — Core value, constraints, key decisions table (13 decisions from domain grilling).
- `.planning/REQUIREMENTS.md` — Full v1.0 requirements with traceability; Phase 1 is AUTH-01..03.

### Milestone Scope
- `.planning/ROADMAP.md` — Phase 1 goal, success criteria, infrastructure non-negotiables, dependency order.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- None yet — repo is completely greenfield. Every component is net-new.

### Established Patterns
- None inherited from prior phases. Phase 1 establishes all patterns.

### Integration Points
- `docker-compose.yml` must expose PostgreSQL on 5432 and test-postgres on 5433 (per D-03)
- `app/core/dependencies.py` is the central wiring point: `get_session`, `get_settings`, `get_current_user` — all Phase 2+ routes will import from here
- `app/db/models.py` must be importable by Alembic `env.py` via `target_metadata = Base.metadata` for autogenerate to work

</code_context>

<specifics>
## Specific Ideas

- Redis blocklist key format: `revoked:{jti}` where JTI is a UUID claim in the JWT payload — predictable, scannable, TTL-able per-token.
- Docker health checks: PostgreSQL uses `pg_isready`, Redis uses `redis-cli ping`, ChromaDB uses HTTP `/api/v1/heartbeat`, FastAPI uses its own `/health` endpoint.
- The `Company` table is bootstrapped by auth phase but populated starting Phase 3 (when tickers are resolved). Phase 1 just creates the table.
- `app/ingestion/section_constants.py` starts with placeholder constants for the sections defined in CONTEXT.md; actual values populated in Phase 2.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 1-foundation-auth*
*Context gathered: 2026-06-27*
