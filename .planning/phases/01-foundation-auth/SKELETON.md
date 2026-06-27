# Walking Skeleton — Vantage

**Phase:** 1
**Generated:** 2026-06-27

## Capability Proven End-to-End

A user registers an account via `POST /api/v1/auth/register`, receives a JWT, authenticates subsequent requests with that JWT (`GET /api/v1/auth/me`), and logs out (`POST /api/v1/auth/logout`) — with the revoked token rejected on retry. This proves Docker Compose stack health, async DB write (user creation via asyncpg), async DB read (user lookup on login), Redis write (blocklist on logout), and the full JWT auth flow end-to-end.

## Architectural Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Framework | Python 3.11 + FastAPI + uvicorn | Required for LangGraph async compatibility; FastAPI's Depends system enables clean test isolation via `dependency_overrides` |
| Data layer | PostgreSQL 16 + SQLAlchemy 2.x async (asyncpg driver) + Alembic | asyncpg is native asyncio — no threadpool wrappers needed in LangGraph coroutines; Alembic enables clean schema versioning across 4 phases |
| Auth | JWT (python-jose, HS256, 24h expiry) + bcrypt (passlib) + Redis 7 blocklist | Stateless JWT enables horizontal scaling; Redis blocklist enforces immediate revocation on logout; bcrypt is industry-standard for password hashing |
| Supporting services | Redis 7 (session/blocklist) + ChromaDB (vector store, Phase 2+) | Redis is dual-purpose: blocklist (Phase 1) and future caching (Phase 2+); ChromaDB added now so Phase 2 doesn't reshape docker-compose |
| Deployment | Docker Compose — 4 services (api, postgres, redis, chromadb) with health checks and `depends_on: condition: service_healthy` | Single command `docker-compose up` for full local stack; health checks prevent FastAPI startup before DB/Redis are ready |
| Directory layout | By-layer under `app/` (`api/v1/`, `agents/`, `services/`, `models/`, `db/`, `ingestion/`, `graph/`, `core/`) | Layer separation enforces: all external API calls in `services/` only; section constants in `ingestion/` only; LangGraph code in `agents/` and `graph/` only |
| Test isolation | `docker-compose.test.yml` with dedicated `test-postgres` on port 5433; pytest-asyncio; `dependency_overrides` for settings/session | Realistic PostgreSQL dialect (not SQLite); independent DB prevents test↔dev DB pollution; `dependency_overrides` avoids `.env` pollution in CI |

## Stack Touched in Phase 1

- [x] Project scaffold: `pyproject.toml`, `requirements/base.txt`, `requirements/dev.txt`, Docker Compose, pytest config
- [x] Routing: `app/api/v1/auth.py` with `/register`, `/login`, `/logout`, `/me` endpoints mounted at `/api/v1`
- [x] Database: asyncpg write (INSERT User on register) + asyncpg read (SELECT User on login)
- [x] "UI" (API): register + login + logout endpoints verified via `httpx.AsyncClient` in pytest-asyncio tests
- [x] Deployment: `docker-compose up` — all 4 services healthy; `docker-compose ps` shows no unhealthy containers

## Out of Scope (Deferred to Later Slices)

- Actual Groq API calls — Phase 1 rate limiter is a stub; no LLM inference
- Actual EDGAR API calls — Phase 1 client enforces headers only; no real SEC requests
- Document ingestion pipeline — Phase 2
- Research request/disambiguation endpoints — Phase 3
- Agent execution and ResearchMemo generation — Phase 4
- Refresh token flow — 24h access token is sufficient for v1.0 skeleton
- WebSocket, Celery, async task queue — Milestone 2
- Role-based access control — single user role only
- Frontend / React UI — curl/httpx sufficient for this API-only project

## Subsequent Slice Plan

Each later phase adds one vertical slice on top of this skeleton without altering its architectural decisions:

- Phase 2: SEC EDGAR auto-ingestion → hybrid RAG (ChromaDB + BM25) → DocumentChunk retrieval
- Phase 3: Free-text research request → ticker disambiguation → ResearchPlan creation
- Phase 4: FundamentalAnalysis + Synthesis agents in LangGraph → structured ResearchMemo with inline citations
