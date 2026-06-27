# Roadmap: Vantage — Milestone v1.0 Walking Skeleton

## Overview

Milestone v1.0 proves the full research pipeline works end-to-end: request → EDGAR ingest → hybrid RAG → 2 agents → ResearchMemo. Four phases, each delivering a complete, verifiable capability. Phases 1–3 build the foundation and intake; Phase 4 fires the pipeline and proves every component is wired together. No WebSocket, no Celery async, no frontend — curl or Postman is sufficient to verify every phase.

## Phases

**Phase Numbering:**

- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Foundation & Auth** - Docker Compose scaffold, JWT auth, Groq rate limiter, EDGAR client, Company entity, section_constants
- [ ] **Phase 2: Document Ingestion Pipeline** - Auto-fetch SEC filings, hybrid RAG (dense + BM25 + reranker), caching, private doc isolation, deduplication
- [ ] **Phase 3: Research Request & Disambiguation** - Free-text intake, confidence-gated ticker extraction, ClarificationResponse, multi-ticker, PDF upload, ResearchPlan
- [ ] **Phase 4: Minimal Agent Run** - FundamentalAnalysis + Synthesis in LangGraph, sync execution, structured cited ResearchMemo end-to-end

## Phase Details

### Phase 1: Foundation & Auth

**Goal**: The project infrastructure is running and users can securely authenticate; all day-one constraints are enforced before any feature code is written
**Mode:** mvp
**Depends on**: Nothing (first phase)
**Requirements**: AUTH-01, AUTH-02, AUTH-03
**Infrastructure non-negotiables** (enforced in this phase, not captured as user-facing requirements):

- Docker Compose scaffold: FastAPI + PostgreSQL + Redis + ChromaDB, all services health-checked
- Company entity with ticker as primary key (all FK references to ticker go through this table)
- Groq shared async rate limiter (token-bucket) — CI fails if any code path calls Groq directly
- EDGAR HTTP client with `Vantage/1.0 vishwak.vel@gmail.com` User-Agent header on every request
- `app/ingestion/section_constants.py` as single source of truth for section string literals

**Success Criteria** (what must be TRUE):

  1. `docker-compose up` starts all four services (FastAPI, PostgreSQL, Redis, ChromaDB) with no errors and all health checks pass
  2. A new user registers via `POST /api/v1/auth/register` with email and password and receives a valid JWT
  3. A registered user logs in via `POST /api/v1/auth/login` and the returned JWT authenticates subsequent protected requests
  4. A logged-in user calls `POST /api/v1/auth/logout` and subsequent requests with that token are rejected with 401
  5. All EDGAR client requests in the test suite carry the `Vantage/1.0 vishwak.vel@gmail.com` User-Agent header (verified via mock assertions)

**Plans**: 5/8 plans executed

Plans:

- [x] 01-01-PLAN.md — Project scaffold: pyproject.toml, requirements/, Dockerfile, docker-compose.yml (4 services + health checks)
- [x] 01-02-PLAN.md — App core structure: Settings config, FastAPI factory, DB base + async session, all 9 ORM models
- [x] 01-03-PLAN.md — Alembic setup: async-compatible env.py, initial migration creating all 9 tables
- [x] 01-04-PLAN.md — Auth service layer: security.py (bcrypt + JWT), Pydantic models, register/login/logout/revoke service functions
- [ ] 01-05-PLAN.md — Auth API endpoints: dependencies.py (get_current_user), auth router (register/login/logout/me), wire into main.py
- [x] 01-06-PLAN.md — Service stubs: Groq async token-bucket rate limiter, EDGAR HTTP client (User-Agent), section_constants.py
- [ ] 01-07-PLAN.md — Auth integration tests: conftest.py fixtures, 12 endpoint tests covering all SPEC ACs and prohibitions
- [ ] 01-08-PLAN.md — Boundary & service tests: import guard, Redis TTL/503, EDGAR User-Agent mock, rate limiter, migration smoke test

### Phase 2: Document Ingestion Pipeline

**Goal**: EDGAR filings are automatically fetched, chunked, and indexed into the hybrid RAG pipeline when a ticker is researched; previously ingested filings are reused; private user PDFs are isolated; deduplication prevents double-ingestion
**Mode:** mvp
**Depends on**: Phase 1
**Requirements**: INGEST-01, INGEST-02, INGEST-03, INGEST-04, INGEST-05
**Success Criteria** (what must be TRUE):

  1. Triggering research for ticker AAPL automatically fetches recent 10-K/10-Q filings from EDGAR and stores indexed chunks in ChromaDB — no manual upload required
  2. A second research trigger for AAPL reuses already-indexed chunks and does not call the EDGAR fetch service (verified via mock call count assertion)
  3. A user's uploaded private PDF returns chunks under that user's metadata filter and returns zero results under any other user's metadata filter on the same ticker
  4. When ingestion from a specific source fails (e.g., EDGAR unreachable), the API returns a non-fatal source warning before the memo is attempted — no 500 error
  5. Uploading the same 10-K as a user PDF alongside an auto-ingested EDGAR copy produces exactly one `DocumentChunk` set in ChromaDB, keyed by canonical_id — no duplicate rows

**Plans**: TBD

### Phase 3: Research Request & Disambiguation

**Goal**: Users can submit any free-text research request; the system auto-resolves high-confidence tickers or surfaces a ClarificationResponse before creating a ResearchPlan; multi-ticker and PDF-augmented requests are supported
**Mode:** mvp
**Depends on**: Phase 2
**Requirements**: REQST-01, REQST-02, REQST-03, REQST-04, REQST-05, REQST-06
**Success Criteria** (what must be TRUE):

  1. `POST /api/v1/research` with "Tell me about Apple" resolves to AAPL (confidence ≥ 0.85) and returns a persisted ResearchPlan ID — no clarification prompt is issued
  2. `POST /api/v1/research` with an ambiguous query (confidence < 0.85) returns a ClarificationResponse with candidate tickers; no ResearchMemo or ResearchPlan row is created until the user resolves
  3. Resubmitting with a ticker selected from the ClarificationResponse creates a ResearchPlan with resolved_tickers populated and proceeds to the ingestion trigger
  4. `POST /api/v1/research` with "Compare AAPL and MSFT" produces a ResearchPlan with both tickers in resolved_tickers
  5. A user attaches a private PDF to a research request; the file is accepted, stored with user-scoped metadata, and its plan_id is returned in the response

**Plans**: TBD

### Phase 4: Minimal Agent Run

**Goal**: A ResearchPlan executes synchronously through FundamentalAnalysis and Synthesis in LangGraph, producing a structured ResearchMemo with inline citations — proving the full pipeline end-to-end with two agents; no WebSocket, no Celery
**Mode:** mvp
**Depends on**: Phase 3
**Requirements**: EXEC-02, EXEC-03, MEMO-01, MEMO-02, MEMO-03
**Success Criteria** (what must be TRUE):

  1. `POST /api/v1/research/{plan_id}/run` for ticker AAPL synchronously returns a ResearchMemo with status COMPLETE or PARTIAL within 60 seconds
  2. The ResearchMemo body contains a named Fundamentals section and a named Synthesis section at minimum (full section set deferred to Milestone 2)
  3. Every factual claim in the Fundamentals section includes a citation object with the source chunk's canonical_id and an inline quoted excerpt from the EDGAR filing
  4. FundamentalAnalysis and Synthesis each report a status of SUCCESS, PARTIAL, or FAILED in the response body
  5. If FundamentalAnalysis returns no output, the pipeline returns a ResearchMemo with status PARTIAL and FundamentalAnalysis status FAILED — no 5xx error is raised

**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation & Auth | 5/8 | In Progress|  |
| 2. Document Ingestion Pipeline | 0/? | Not started | - |
| 3. Research Request & Disambiguation | 0/? | Not started | - |
| 4. Minimal Agent Run | 0/? | Not started | - |
