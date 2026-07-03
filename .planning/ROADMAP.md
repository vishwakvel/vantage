# Roadmap: Vantage — Milestone v1.0 Walking Skeleton

## Overview

Milestone v1.0 proves the full research pipeline works end-to-end: request → EDGAR ingest → hybrid RAG → 2 agents → ResearchMemo. Four phases, each delivering a complete, verifiable capability. Phases 1–3 build the foundation and intake; Phase 4 fires the pipeline and proves every component is wired together. No WebSocket, no Celery async, no frontend — curl or Postman is sufficient to verify every phase.

## Phases

**Phase Numbering:**

- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: Foundation & Auth** - Docker Compose scaffold, JWT auth, Groq rate limiter, EDGAR client, Company entity, section_constants (completed 2026-06-27)
- [x] **Phase 2: Document Ingestion Pipeline** - Auto-fetch SEC filings, hybrid RAG (dense + BM25 + reranker), caching, private doc isolation, deduplication (completed 2026-06-28)
- [x] **Phase 3: Research Request & Disambiguation** - Free-text intake, confidence-gated ticker extraction, ClarificationResponse, multi-ticker, PDF upload, ResearchPlan (completed 2026-07-01)
- [x] **Phase 4: Minimal Agent Run** - FundamentalAnalysis + Synthesis in LangGraph, sync execution, structured cited ResearchMemo end-to-end (completed 2026-07-02)

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

**Plans**: 7/8 plans executed

Plans:

- [x] 01-01-PLAN.md — Project scaffold: pyproject.toml, requirements/, Dockerfile, docker-compose.yml (4 services + health checks)
- [x] 01-02-PLAN.md — App core structure: Settings config, FastAPI factory, DB base + async session, all 9 ORM models
- [x] 01-03-PLAN.md — Alembic setup: async-compatible env.py, initial migration creating all 9 tables
- [x] 01-04-PLAN.md — Auth service layer: security.py (bcrypt + JWT), Pydantic models, register/login/logout/revoke service functions
- [x] 01-05-PLAN.md — Auth API endpoints: dependencies.py (get_current_user), auth router (register/login/logout/me), wire into main.py
- [x] 01-06-PLAN.md — Service stubs: Groq async token-bucket rate limiter, EDGAR HTTP client (User-Agent), section_constants.py
- [x] 01-07-PLAN.md — Auth integration tests: conftest.py fixtures, 12 endpoint tests covering all SPEC ACs and prohibitions
- [x] 01-08-PLAN.md — Boundary & service tests: import guard, Redis TTL/503, EDGAR User-Agent mock, rate limiter, migration smoke test

### Phase 2: Document Ingestion Pipeline

**Goal**: As a retail investor, I want to have SEC filings auto-fetched, chunked, and indexed for hybrid RAG retrieval, with reuse, private-doc isolation, and dedup, so that my research memo is grounded in accurate, up-to-date source documents without me lifting a finger.
**Mode:** mvp
**Depends on**: Phase 1
**Requirements**: INGEST-01, INGEST-02, INGEST-03, INGEST-04, INGEST-05
**Success Criteria** (what must be TRUE):

  1. Triggering research for ticker AAPL automatically fetches recent 10-K/10-Q filings from EDGAR and stores indexed chunks in ChromaDB — no manual upload required
  2. A second research trigger for AAPL reuses already-indexed chunks and does not call the EDGAR fetch service (verified via mock call count assertion)
  3. A user's uploaded private PDF returns chunks under that user's metadata filter and returns zero results under any other user's metadata filter on the same ticker
  4. When ingestion from a specific source fails (e.g., EDGAR unreachable), the API returns a non-fatal source warning before the memo is attempted — no 500 error
  5. Uploading the same 10-K as a user PDF alongside an auto-ingested EDGAR copy produces exactly one `DocumentChunk` set in ChromaDB, keyed by canonical_id — no duplicate rows

**Plans**: 6/6 plans complete

Plans:
**Wave 1**

- [x] 02-01-PLAN.md — Foundation: pin deps (legitimacy checkpoint), ChromaDB config, vector_store singleton with user-isolation
- [x] 02-02-PLAN.md — EDGAR get_archive() + section-aware chunker (≤250-word, section_constants)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 02-03-PLAN.md — ingestion_service.ingest_ticker: EDGAR fetch → chunk → embed → store, canonical_id dedup, non-fatal warnings
- [x] 02-04-PLAN.md — Hybrid retriever: dense + BM25 + RRF(k=60) + cross-encoder rerank, user-scoped

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 02-05-PLAN.md — ingestion_service.ingest_pdf: PyMuPDF, user-scoped storage, isolation + cross-source dedup proof

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 02-06-PLAN.md — Ingest API router (POST /api/v1/ingest/ticker, /pdf) wired into v1 aggregator

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

**Plans**: 4/4 plans complete

Plans:
**Wave 1**

- [x] 03-01-PLAN.md — Free-text request → resolve single ticker (exact+fuzzy, difflib) → persist ResearchPlan → trigger ingestion (REQST-01, REQST-02)

**Wave 2** *(blocked on Wave 1)*

- [x] 03-02-PLAN.md — Ambiguous → ClarificationResponse (no plan/memo) + LLM fallback + resubmit-with-selection (REQST-03, REQST-04)

**Wave 3** *(blocked on Wave 2)*

- [x] 03-03-PLAN.md — Multi-ticker "Compare AAPL and MSFT" → single plan with both tickers, all-or-nothing gating, cap at 2 (REQST-05)

**Wave 4** *(blocked on Wave 3)*

- [x] 03-04-PLAN.md — Attach private PDF via POST /research/{plan_id}/documents (ownership-checked, user-scoped) (REQST-06)

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

**Plans**: 5/5 plans complete

Plans:
**Wave 1**

- [x] 04-01-PLAN.md — Groq LLM integration: real call_groq (llama-3.3-70b-versatile, rate-limited), pin langgraph+groq, required GROQ_API_KEY (legitimacy checkpoint) (EXEC-02)

**Wave 2** *(blocked on Wave 1)*

- [x] 04-02-PLAN.md — FundamentalAnalysis agent: retrieve→LLM→cited narrative, SUCCESS/PARTIAL/FAILED, AgentTask+AgentOutput, never-raise (MEMO-02, MEMO-03, EXEC-02)

**Wave 3** *(blocked on Wave 2)*

- [x] 04-03-PLAN.md — Synthesis agent: overall investment take, owns ResearchMemo.status via _compute_memo_status (EXEC-02, EXEC-03)

**Wave 4** *(blocked on Wave 3)*

- [x] 04-04-PLAN.md — LangGraph 2-node linear StateGraph (fundamental_analysis→synthesis) + AgentGraphState (EXEC-03)

**Wave 5** *(blocked on Wave 4)*

- [x] 04-05-PLAN.md — POST /research/{plan_id}/run: ownership-checked, runs graph, named-section cited memo, PARTIAL-on-failure, parent_memo_id re-run lineage (EXEC-02, EXEC-03, MEMO-01)

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation & Auth | 8/8 | Complete    | 2026-06-27 |
| 2. Document Ingestion Pipeline | 6/6 | Complete   | 2026-06-28 |
| 3. Research Request & Disambiguation | 4/4 | Complete    | 2026-07-01 |
| 4. Minimal Agent Run | 5/5 | Complete    | 2026-07-02 |
