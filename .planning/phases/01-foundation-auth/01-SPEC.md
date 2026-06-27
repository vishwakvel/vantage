# Phase 1: Foundation & Auth — Specification

**Created:** 2026-06-27
**Ambiguity score:** 0.162 (gate: ≤ 0.20)
**Requirements:** 9 locked

## Goal

The project runs in Docker with all four services healthy, users can securely authenticate via JWT, and all day-one architectural constraints (Groq rate limiter, EDGAR User-Agent, Company entity, section_constants) are enforced before any feature code is written.

## Background

The repo is completely greenfield — only `CLAUDE.md`, `CONTEXT.md`, `docs/PRD.md`, and `pyrefly.toml` exist. No Python files, no `app/` directory, no Docker Compose, no migrations, no `requirements.txt`. Everything is built from scratch in this phase. Phase 1 is the prerequisite for all subsequent phases: Phase 2 depends on a running PostgreSQL + ChromaDB + Redis stack, and Phase 3 depends on an authenticated API surface.

## Requirements

1. **Docker Compose scaffold**: All four services start and pass health checks.
   - Current: No `docker-compose.yml` exists; no services run
   - Target: `docker-compose.yml` defines FastAPI, PostgreSQL 16, Redis 7, and ChromaDB; all pass health checks; FastAPI starts only after the other three report healthy via `depends_on: condition: service_healthy`
   - Acceptance: `docker-compose up` exits with all four services healthy; `docker-compose ps` shows no unhealthy containers

2. **Full v1.0 schema via Alembic**: All 9 domain tables are created by migrations on a fresh database.
   - Current: No SQLAlchemy models, no Alembic config, no tables exist
   - Target: SQLAlchemy models for `users`, `companies`, `documents`, `document_chunks`, `research_requests`, `research_plans`, `research_memos`, `agent_tasks`, `agent_outputs`; Alembic initial migration creates all 9 tables cleanly
   - Acceptance: `alembic upgrade head` on a fresh PostgreSQL instance creates all 9 tables with no errors; `alembic downgrade base` drops them cleanly

3. **User registration**: New user can register with email and password.
   - Current: No auth endpoints exist; no `users` table
   - Target: `POST /api/v1/auth/register` accepts `{email, password}`, stores the user (bcrypt-hashed password), returns `{access_token, token_type: "bearer"}`; duplicate email → 409
   - Acceptance: Registering with a new email returns 200 and a decodable JWT; registering the same email again returns 409; response body contains no `password` or `password_hash` field

4. **User login**: Registered user authenticates and receives a JWT.
   - Current: No login endpoint
   - Target: `POST /api/v1/auth/login` accepts `{email, password}`, verifies credentials, returns JWT with 24h expiry; wrong credentials → 401
   - Acceptance: Correct credentials return a JWT that authenticates `GET /api/v1/auth/me`; wrong credentials return 401; JWT `exp - iat ≤ 86700` seconds (24h + 5min tolerance)

5. **User logout**: Logged-in user can revoke their JWT.
   - Current: No logout endpoint; no token revocation mechanism
   - Target: `POST /api/v1/auth/logout` (requires valid JWT) adds the token to a Redis blocklist with TTL = `max(0, token_exp - now)`; subsequent requests with that token return 401; if Redis is unreachable, logout returns 503 rather than 200
   - Acceptance: Token accepted before logout, rejected 401 after; Redis blocklist TTL ≥ remaining token lifetime at revocation time; Redis unavailability returns 503

6. **Groq rate limiter**: Shared async token-bucket enforced at the service boundary.
   - Current: No Groq client or rate limiter exists
   - Target: `app/services/groq_client.py` contains a shared async token-bucket rate limiter (~6,000 tokens/min); when the bucket is at 0, callers block (await) until tokens replenish — no request is ever dropped; an import guard test in `tests/` asserts no module in `app/agents/` or `app/graph/` directly imports the Groq client
   - Acceptance: Import guard test passes; a unit test demonstrates that a call arriving at 0-token bucket awaits rather than raising immediately; test suite fails if any agent module imports groq directly

7. **EDGAR HTTP client**: Every EDGAR request carries the required User-Agent header.
   - Current: No EDGAR client exists
   - Target: `app/services/edgar_client.py` implements the EDGAR HTTP client; every request sets `User-Agent: Vantage/1.0 vishwak.vel@gmail.com`; no request omits this header
   - Acceptance: Mock-based test asserts that every HTTP call made by `edgar_client` contains the `User-Agent: Vantage/1.0 vishwak.vel@gmail.com` header; test fails if any call omits it

8. **section_constants.py**: Single source of truth for all section string literals.
   - Current: No `app/ingestion/` directory; no `section_constants.py`
   - Target: `app/ingestion/section_constants.py` defines all section name constants as module-level string variables; all constants are non-empty strings; no inline section name literals appear elsewhere in the codebase
   - Acceptance: Unit test imports `section_constants` and asserts all public constants are `isinstance(v, str) and len(v) > 0`; no inline section string literals exist outside this file (enforced by convention; verified by code review)

9. **Dev tooling**: Project has working linting, type checking, and test infrastructure.
   - Current: Only `pyrefly.toml` exists for type checking; no `pyproject.toml`, no `requirements.txt`, no pytest config
   - Target: `pyproject.toml` with ruff and black configured; `requirements.txt` split into `requirements/base.txt` (runtime) and `requirements/dev.txt` (test + lint); `pytest` configured with `testpaths = ["tests"]`; all linters pass on Phase 1 codebase
   - Acceptance: `ruff check .` exits 0; `black --check .` exits 0; `pytest tests/ -v` exits 0; `pyrefly check` exits 0

## Boundaries

**In scope:**
- `docker-compose.yml` with FastAPI, PostgreSQL 16, Redis 7, ChromaDB — all with health checks and `depends_on` ordering
- SQLAlchemy ORM models for all 9 v1.0 domain tables
- Alembic configuration and initial migration (upgrade + downgrade)
- `app/api/v1/auth/` — register, login, logout endpoints
- JWT generation and validation (python-jose or PyJWT; 24h access token; no refresh token)
- Redis-based JWT blocklist in `app/services/auth_service.py`
- `app/services/groq_client.py` — async token-bucket rate limiter (token tracking only; no actual Groq calls in Phase 1)
- `app/services/edgar_client.py` — HTTP client stub with User-Agent enforcement (no EDGAR API calls in Phase 1)
- `app/ingestion/section_constants.py` — initial set of section name constants (populated in Phase 2)
- `app/db/` — SQLAlchemy base, session factory, Alembic env
- `pyproject.toml`, `requirements/base.txt`, `requirements/dev.txt`, pytest config
- Import guard test for Groq boundary enforcement
- Unit and integration tests for all auth endpoints and service-layer constraints

**Out of scope:**
- Actual Groq API calls — Phase 1 rate limiter is a stub; no LLM calls made
- Actual EDGAR API calls — Phase 1 client is stub; called in Phase 2
- Any ingestion logic — Phase 2
- Research request / disambiguation endpoints — Phase 3
- Agent execution / ResearchMemo — Phase 4
- WebSocket, Celery, async task queue — Milestone 2
- Refresh token flow — 24h access token is sufficient for walking skeleton
- Frontend — curl/Postman sufficient to verify Phase 1
- Role-based access control — single user role only

## Constraints

- Python 3.11 — matches `pyrefly.toml` config and is required for LangGraph compatibility
- FastAPI + SQLAlchemy + Alembic — established in CONTEXT.md; no substitutions
- PostgreSQL 16 — Alembic dialect must target this version
- Redis 7 — blocklist TTL uses Redis SETEX; no Lua scripts needed
- Groq rate limiter: `~6,000 tokens/min` bucket capacity, async (asyncio-compatible) — all callers are async FastAPI/LangGraph coroutines
- Password hashing: bcrypt via `passlib[bcrypt]` — industry standard, not argon2 (not yet in requirements.txt)
- JWT: `python-jose[cryptography]` or `PyJWT` — HS256 algorithm; secret from `.env`; never hardcoded
- No secrets committed — all API keys, DB passwords, JWT secret via `.env` (gitignored)
- EDGAR User-Agent string is exact: `Vantage/1.0 vishwak.vel@gmail.com` — EDGAR policy requires this
- Company entity: `companies.ticker` is the primary key; all FK references to ticker go through this table — established before any other table with a ticker FK

## Acceptance Criteria

- [ ] `docker-compose up` starts all four services (FastAPI, PostgreSQL, Redis, ChromaDB) with no errors and all health checks pass
- [ ] FastAPI service starts only after PostgreSQL, Redis, and ChromaDB report healthy (`depends_on: condition: service_healthy`)
- [ ] `alembic upgrade head` on a fresh database creates all 9 tables (`users`, `companies`, `documents`, `document_chunks`, `research_requests`, `research_plans`, `research_memos`, `agent_tasks`, `agent_outputs`) with no errors
- [ ] `alembic downgrade base` drops all tables cleanly
- [ ] `POST /api/v1/auth/register` with a new email+password returns 200 and a decodable JWT
- [ ] `POST /api/v1/auth/register` with a duplicate email returns 409
- [ ] Registration response body does NOT contain a `password` or `password_hash` field
- [ ] `POST /api/v1/auth/login` with correct credentials returns a JWT that authenticates `GET /api/v1/auth/me`
- [ ] `POST /api/v1/auth/login` with wrong credentials returns 401
- [ ] JWT `exp - iat ≤ 86700` seconds (24h + 5min tolerance)
- [ ] `POST /api/v1/auth/logout` with a valid JWT: subsequent request with that token returns 401
- [ ] Redis blocklist TTL for a revoked token ≥ remaining token lifetime at revocation time
- [ ] `POST /api/v1/auth/logout` returns 503 when Redis is unreachable
- [ ] Import guard test passes: no module in `app/agents/` or `app/graph/` directly imports the Groq client; test fails if boundary is violated
- [ ] Token-bucket rate limiter blocks (awaits) at 0-token state — no request is dropped
- [ ] Every HTTP call made by `edgar_client` in tests carries `User-Agent: Vantage/1.0 vishwak.vel@gmail.com` (verified by mock assertion)
- [ ] Unit test asserts all public constants in `section_constants.py` are non-empty strings
- [ ] `ruff check .` exits 0 on Phase 1 codebase
- [ ] `black --check .` exits 0 on Phase 1 codebase
- [ ] `pytest tests/ -v` exits 0

## Edge Coverage

**Coverage:** 5 covered/backstop · 14 dismissed · 0 unresolved

| Category | Requirement | Status | Resolution / Reason |
|----------|-------------|--------|---------------------|
| idempotency | AUTH-01 | 🧪 backstop | DB UNIQUE constraint on `users.email` catches concurrent duplicate registration; test asserts 409 on duplicate email |
| concurrency | AUTH-01 | 🧪 backstop | Same DB UNIQUE constraint covers the race — one INSERT wins, other gets IntegrityError → 409 |
| ordering | INFRA-01 | ✅ covered | `depends_on: condition: service_healthy` in docker-compose enforces startup order; covered by AC #2 |
| unclassified | INFRA-02 | 🧪 backstop | Test that runs `alembic upgrade head` on a fresh test DB and asserts all 9 tables exist; held-out migration test |
| boundary | INFRA-03 | ✅ covered | At 0 tokens, rate limiter blocks (awaits) — never drops; covered by AC #15 |
| precision | INFRA-03 | ⛔ dismissed | Float rounding in token counting is sub-token at 6,000 tok/min scale; not a meaningful edge for v1.0 |
| empty | INFRA-05 | ✅ covered | Test asserts all constants in section_constants.py are non-empty strings; covered by AC #17 |
| concurrency | AUTH-02 | ⛔ dismissed | Login creates no shared write state; JWT generation is a pure function of credentials + secret |
| concurrency | AUTH-03 | ⛔ dismissed | Redis SET is idempotent; simultaneous logouts of the same token produce identical result |
| adjacency | INFRA-01 | ⛔ dismissed | Docker service startup is sequential with health checks; interval/adjacency semantics don't apply |
| empty | INFRA-01 | ⛔ dismissed | Docker Compose services are predefined; no variable-length input |
| adjacency | INFRA-04 | ⛔ dismissed | HTTP header presence is boolean; interval/adjacency semantics don't apply |
| empty | INFRA-04 | ⛔ dismissed | Requirement mandates header is always present; mock assertion catches absent case |
| ordering | INFRA-04 | ⛔ dismissed | HTTP headers are unordered; User-Agent presence is independent of header ordering |
| concurrency | INFRA-04 | ⛔ dismissed | Each EDGAR HTTP request is independent; header inclusion is per-request, not shared state |
| adjacency | INFRA-05 | ⛔ dismissed | Section constants are discrete string identifiers; interval/adjacency semantics don't apply |
| encoding | INFRA-05 | ⛔ dismissed | Section names are ASCII identifiers; multi-byte encoding semantics don't apply |
| ordering | INFRA-05 | ⛔ dismissed | Module-level constants; ordering has no semantic meaning |
| adjacency | INFRA-06 | ⛔ dismissed | Static tooling configuration; no interval operations |
| empty | INFRA-06 | ⛔ dismissed | Static configuration; no variable-length input |
| ordering | INFRA-06 | ⛔ dismissed | Lint/test config; result ordering irrelevant |

## Prohibitions (must-NOT)

**Coverage:** 4/4 applicable prohibitions resolved · 0 unresolved

| Prohibition (must-NOT statement) | Requirement | Status | Verification |
|----------------------------------|-------------|--------|--------------|
| MUST NOT include `password` or `password_hash` in registration response body | AUTH-01 | resolved | test — assert response JSON keys do not contain `password` or `password_hash`; check_kind: node-test, check_target: tests/api/test_auth.py |
| MUST NOT issue a JWT with `exp - iat > 86700` seconds or with no expiry | AUTH-02 | resolved | test — decode returned JWT and assert `exp - iat ≤ 86700`; check_kind: node-test, check_target: tests/api/test_auth.py |
| MUST NOT set Redis blocklist TTL shorter than the token's remaining lifetime | AUTH-03 | resolved | test — revoke a token with known expiry, assert Redis key TTL ≥ remaining lifetime; check_kind: node-test, check_target: tests/services/test_auth_service.py |
| MUST NOT return 2xx from logout when Redis is unreachable (leaves token active) | AUTH-03 | resolved | judgment — code must raise and propagate Redis connection error as 503; verified by code review + test that mocks Redis failure |

**Canon breadcrumbs (not minted — owned by /gsd-secure-phase):**
- *Plaintext password storage → OWASP A02 (Cryptographic Failures) — owned by /gsd-secure-phase + passlib[bcrypt]*
- *Brute-force login → OWASP A07 — owned by /gsd-secure-phase*
- *JWT "none" algorithm → OWASP A02 — owned by /gsd-secure-phase*
- *SQL injection in auth inputs → OWASP A03 — owned by SQLAlchemy parameterized queries + /gsd-secure-phase*

## Ambiguity Report

| Dimension           | Score | Min  | Status | Notes |
|---------------------|-------|------|--------|-------|
| Goal Clarity        | 0.85  | 0.75 | ✓      | Goal is specific: infrastructure up + auth working + 5 day-one constraints enforced |
| Boundary Clarity    | 0.85  | 0.70 | ✓      | Full v1.0 schema in Phase 1 confirmed; dev tooling in scope confirmed; clear out-of-scope list |
| Constraint Clarity  | 0.82  | 0.65 | ✓      | JWT 24h expiry, Redis blocklist, bcrypt, async token-bucket, Python 3.11, exact EDGAR User-Agent string |
| Acceptance Criteria | 0.82  | 0.70 | ✓      | 20 pass/fail criteria; all falsifiable |
| **Ambiguity**       | 0.162 | ≤0.20| ✓      | Gate passed after 2 rounds |

## Interview Log

| Round | Perspective        | Question summary                              | Decision locked |
|-------|--------------------|-----------------------------------------------|-----------------|
| 1     | Researcher         | Which SQLAlchemy models does Phase 1 create?  | Full v1.0 schema (9 tables) — all in Phase 1 via Alembic |
| 1     | Researcher         | How is Groq rate limiter "CI enforced"?       | Import guard test in tests/ — fails if agents import groq directly |
| 1     | Researcher         | JWT expiry and refresh strategy?              | Long-lived 24h access token; no refresh token in v1.0 |
| 2     | Researcher + Simplifier | Logout token revocation mechanism?       | Redis blocklist; TTL = remaining token lifetime; 503 if Redis down |
| 2     | Researcher + Simplifier | Dev tooling in scope?                    | Yes — pyproject.toml, ruff+black, pytest config, split requirements |
| Edge  | Failure Analyst    | Duplicate registration race condition?        | DB UNIQUE + 409 response; backstop test |
| Edge  | Researcher         | Docker service startup ordering?              | depends_on condition: service_healthy; cover in AC |
| Edge  | Boundary Keeper    | Rate limiter at 0 tokens: block or drop?      | Block (await) — no request is ever dropped |
| Edge  | Researcher         | Migration test needed?                        | Backstop: alembic upgrade head on fresh test DB |
| Edge  | Researcher         | Empty string in section_constants.py?         | Specify: test asserts all constants are non-empty |

---

*Phase: 01-foundation-auth*
*Spec created: 2026-06-27*
*Next step: /gsd-discuss-phase 1 — implementation decisions (file structure, library choices, etc.)*
