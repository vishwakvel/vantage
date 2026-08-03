# Milestones

## v2.0 Full Agent Suite & Observability (Shipped: 2026-08-03)

**Phases completed:** 8 phases, 68 plans, 142 tasks

**Key accomplishments:**

- **Phase 5:** All 7 agents (Fundamentals, Sentiment, Risk, Macro/Sector, Comparables) run in true concurrent fan-out with Synthesis fan-in degrading gracefully to a PARTIAL memo with explicit per-section failure reasons.
- **Phase 6:** Research runs asynchronously as a Celery task with a live per-agent WebSocket progress panel; a user can navigate away and return to a completed memo.
- **Phase 7:** Synthesis emits a structured, severity-rated Contradictions panel alongside an at-a-glance COMPLETE/PARTIAL memo status.
- **Phase 8:** Persistent, resumable follow-up chat grounded strictly on memo text and conversation history (no RAG re-query), flagging questions that exceed memo coverage.
- **Phase 9:** Financial metrics persisted per (ticker, metric, period) with per-metric isolation-forest anomaly detection surfaced as severity-rated items in the fundamentals section.
- **Phase 10:** Personal watchlist with NEW_FILING, PRICE_MOVE, and SCHEDULED alert rules, each independently enable/disable-able.
- **Phase 11:** Scheduled Celery beat evaluation of alert rules, in-app notifications via WebSocket, and per-ticker alert history.
- **Phase 12:** Every agent call traced in LangSmith; per-agent token + external API costs aggregate to a per-memo total shown in the memo header; daily RAGAS offline eval scores retrieval quality against a golden set.

**Stats:** 149 commits, 134 files changed (+27,618/-485 lines), 29 days (2026-07-05 → 2026-08-03)

**Known verification overrides:** 1 — Phase 8's SUMMARY.md files for plans 08-01/03/04/06 were lost during a worktree merge (`.planning/` is gitignored); implementation is independently confirmed via `08-VERIFICATION.md` (passed, 4/4 criteria) and matching git history. See STATE.md Deferred Items.

---

## v1.0 Walking Skeleton (Shipped: 2026-07-03)

**Phases completed:** 4 phases, 23 plans, 42 tasks

**Key accomplishments:**

- Project scaffold with pyproject.toml (ruff/black/pytest), pinned requirements split into base/dev, Dockerfile for python:3.11-slim, and 4-service Docker Compose with health-checked postgres:16, redis:7, chromadb:0.5.0, and api depending on all three via service_healthy.
- FastAPI application factory with pydantic-settings config, lazy async SQLAlchemy session, and all 9 ORM models using UUID PKs, str+Enum status columns, and soft-delete on ResearchMemo.
- Async Alembic env wired to all 9 SQLAlchemy models; hand-written initial migration creates all 9 tables in FK-safe order with UUID PKs, PostgreSQL enum types, and clean downgrade path
- bcrypt password hashing (direct library), HS256 JWT 3-tuple tokens, Pydantic auth models, and register/login/logout/revoke service with Redis blocklist and 503 guard.
- FastAPI Bearer auth dependency chain (get_redis → is_token_revoked → get_current_user) with register/login/logout/me endpoints mounted at /api/v1/auth.
- Async token-bucket rate limiter (6000 tok/min, blocks-never-drops) and EDGAR client (httpx User-Agent enforced at transport level) and 17-constant section_constants module — all three day-one architectural boundaries established before any feature code
- pytest conftest.py with async db_session/async_client fixtures + 12-function integration test suite covering all auth endpoints via httpx AsyncClient with ASGI transport.
- Import guard (pkgutil.walk_packages CI boundary), section_constants unit tests, migration smoke test (9-table verify), and full lint pass for `ruff check .` and `black --check .`.
- Lazy ChromaDB 0.5.23 singleton with all-MiniLM-L6-v2 embeddings, mandatory user_id where-filter isolation (INGEST-03), and None-metadata guard.
- Dual-client EDGARClient with `get_archive()` for www.sec.gov Archives downloads, plus `section_aware_chunk()` that regex-splits EDGAR HTML into <=250-word overlapping chunks tagged with `section_constants` values.
- Public-filing ingestion slice with sha256 dedup, ChromaDB embedding, PostgreSQL persistence, and non-fatal EDGAR failure handling.
- Hybrid retrieval via dense (ChromaDB) + BM25 candidate-set RRF(k=60) + cross-encoder reranker, with structural user_id scoping for INGEST-03 isolation.
- `ingest_pdf` with PyMuPDF text extraction, 50 MB DoS guard, and user-scoped chunk storage; cross-user isolation and cross-source dedup proved via monkeypatched seams.
- 1. [Rule 1 - Bug] Form fields required `Form(...)` annotation on PDF endpoint
- Exact + fuzzy ticker resolution (stdlib difflib, no LLM) wired to a new POST /api/v1/research endpoint that persists a ResearchPlan and triggers non-fatal EDGAR ingestion.
- LLM-fallback ticker extraction (rate-limited via groq_client.call_groq, degrades gracefully) plus a strict clarification/resubmit gate: ambiguous requests now create zero ResearchPlan/ResearchMemo rows and a selected_tickers resubmit resolves straight to a persisted plan.
- Free-text "Compare X and Y" queries split into up to 2 terms on comparison connectors, each independently resolved through the existing exact/fuzzy/LLM cascade, with a strict all-or-nothing gate and a hard 2-ticker cap enforced before any resolve/ingest work.
- A dedicated `POST /api/v1/research/{plan_id}/documents` endpoint that ownership-checks the plan (404 on IDOR), size-guards the upload (413), and reuses Phase 2's `ingestion_service.ingest_pdf` with `user_id` sourced exclusively from the authenticated principal.
- Real AsyncGroq-backed call_groq defaulting to llama-3.3-70b-versatile, with GROQ_API_KEY now a required Settings field
- FundamentalAnalysis LangGraph node — retrieves user-scoped chunks via hybrid_retrieve, calls Groq for a cited narrative, and persists AgentTask/AgentOutput with a SUCCESS/PARTIAL/FAILED status driven by section coverage
- Synthesis LangGraph node — reads FundamentalAnalysis's output from graph state, calls Groq for a distinct overall investment take, and computes ResearchMemo.status (memo_status) via the locked D-02 rule so a fundamentals FAILED never masks as COMPLETE
- Compiled 2-node linear LangGraph StateGraph (FundamentalAnalysis -> Synthesis) plus the shared AgentGraphState TypedDict, wiring the two existing agent node functions into a runnable pipeline
- POST /api/v1/research/{plan_id}/run — the phase-closing end-to-end slice that ownership-checks the plan, invokes the compiled FundamentalAnalysis -> Synthesis graph, persists a named-section cited ResearchMemo with parent_memo_id re-run lineage, and returns per-agent statuses alongside the memo status

---
