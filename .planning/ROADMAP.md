# Roadmap: Vantage

## Overview

Vantage is built in nine vertical slices, each delivering a complete, demoable capability. The journey starts with auth and foundational constraints (Groq rate limiter, EDGAR User-Agent, Company entity), then layers in the RAG ingestion pipeline, research request handling, LangGraph agent fan-out, memo rendering with full citations, anomaly-annotated metrics, persistent follow-up chat, and finally the watchlist and alert system with RAGAS quality evaluation. Every phase either extends what came before or unlocks a new user workflow end-to-end.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Foundation & Auth** - JWT auth, Docker Compose stack, Groq rate limiter, EDGAR User-Agent, Company entity
- [ ] **Phase 2: Document Ingestion Pipeline** - Auto-fetch SEC filings, public-doc caching, private PDF ownership, deduplication
- [ ] **Phase 3: Research Request & Disambiguation** - Free-text intake, ticker extraction, clarification flow, multi-ticker and PDF upload
- [ ] **Phase 4: Agent Execution & Live Progress** - LangGraph fan-out, WebSocket progress panel, Celery async, graceful degradation, LangSmith traces
- [ ] **Phase 5: ResearchMemo — Reading, Citations & Cost** - Named sections, inline citations with excerpts, Contradictions panel, status, cost breakdown
- [ ] **Phase 6: Financial Metrics & Anomaly Detection** - FinancialMetric upsert table, isolation-forest anomaly detection, severity-rated findings
- [ ] **Phase 7: Follow-Up Chat Session** - Persistent memo-grounded chat, cross-session history, out-of-scope escalation
- [ ] **Phase 8: Watchlist Core & NEW_FILING Alert** - Watchlist CRUD, thesis attachment, NEW_FILING trigger, alert history, pause/resume
- [ ] **Phase 9: Price & Scheduled Alerts, Lineage & RAGAS** - PRICE_MOVE and SCHEDULED alert rules, parent_memo_id lineage chain, offline RAGAS eval

## Phase Details

### Phase 1: Foundation & Auth
**Goal**: Authenticated users can access Vantage; all day-one constraints are enforced — shared Groq async rate limiter, EDGAR User-Agent header, Company entity as ticker PK, section_constants.py, and Docker Compose environment.
**Mode:** mvp
**Depends on**: Nothing (first phase)
**Requirements**: AUTH-01, AUTH-02, AUTH-03
**Success Criteria** (what must be TRUE):
  1. A new user can register with email and password and receive a JWT.
  2. A returning user can log in and the JWT is valid across browser refreshes and new tabs.
  3. A user can log out from any page and is redirected to the login screen with the JWT invalidated.
  4. Any code path that calls Groq directly (bypassing the shared rate limiter) fails in CI — the limiter is the only entry point.
  5. Every EDGAR HTTP request in tests carries the required User-Agent header; requests without it are rejected at the client level.
**Plans**: TBD
**UI hint**: yes

### Phase 2: Document Ingestion Pipeline
**Goal**: Researching any ticker automatically fetches and indexes SEC filings and other public sources into ChromaDB; user PDFs are stored privately; deduplication prevents repeated ingestion of the same filing.
**Mode:** mvp
**Depends on**: Phase 1
**Requirements**: INGEST-01, INGEST-02, INGEST-03, INGEST-04, INGEST-05
**Success Criteria** (what must be TRUE):
  1. Triggering research for a ticker automatically ingests its 10-K, 10-Q, and 8-K filings from EDGAR without any user action beyond submitting the request.
  2. Re-triggering research for the same ticker reuses already-indexed documents and does not re-fetch from EDGAR (verified by checking fetch call count in tests).
  3. A user's uploaded PDF is returned in their own retrieval queries but returns no results for a different user's queries on the same ticker.
  4. When ingestion from a specific source (e.g., NewsAPI) fails, the failure is surfaced to the user as a named source warning before they read the memo.
  5. Uploading the same public SEC filing via both the EDGAR API path and a manual PDF upload stores exactly one document row, identified by canonical_id.
**Plans**: TBD

### Phase 3: Research Request & Disambiguation
**Goal**: Users can submit any free-text research question; unambiguous tickers proceed automatically, ambiguous ones trigger a clarification panel, and multi-ticker and PDF-augmented requests are supported.
**Mode:** mvp
**Depends on**: Phase 1, Phase 2
**Requirements**: REQST-01, REQST-02, REQST-03, REQST-04, REQST-05, REQST-06
**Success Criteria** (what must be TRUE):
  1. Typing "What is Apple's revenue outlook?" resolves to AAPL (confidence ≥ 0.85) and proceeds to research without prompting the user.
  2. Typing an ambiguous query (confidence < 0.85) surfaces a disambiguation panel listing candidate tickers with exchange suffixes; no ResearchMemo row is created until the user selects one.
  3. Selecting a candidate from the disambiguation panel relocks the ticker as resolved_tickers and continues to research automatically.
  4. Entering "Compare AAPL and MSFT" submits a two-ticker research request that ingests and researches both tickers in a single run.
  5. Attaching a PDF during request submission includes that private document as an additional context source for the resulting memo.
**Plans**: TBD
**UI hint**: yes

### Phase 4: Agent Execution & Live Progress
**Goal**: After a request is submitted, six specialist agents execute in a parallel LangGraph fan-out over a Celery worker; the user watches each agent's status in real time via WebSocket; every agent call is traced in LangSmith.
**Mode:** mvp
**Depends on**: Phase 2, Phase 3
**Requirements**: EXEC-01, EXEC-02, EXEC-03, EXEC-04, EXEC-05, OBS-01
**Success Criteria** (what must be TRUE):
  1. The live progress panel shows each of the six specialist agents transitioning from PENDING to SUCCESS, PARTIAL, or FAILED in real time as events arrive over WebSocket.
  2. If one specialist agent fails mid-run, the remaining agents continue and the memo completes in PARTIAL state rather than aborting the entire run.
  3. Every FAILED agent slot in the resulting memo shows an explicit, human-readable reason — it is never silently blank.
  4. A user who navigates away after submitting a request and returns five minutes later finds the memo either completed or still running, with progress intact.
  5. Every LangGraph agent invocation produces a trace in LangSmith that includes full inputs and outputs, viewable in the LangSmith dashboard.
**Plans**: TBD
**UI hint**: yes

### Phase 5: ResearchMemo — Reading, Citations & Cost
**Goal**: Users can read a fully structured ResearchMemo with named collapsible sections, inline citations showing the exact source excerpt, a Contradictions panel with severity ratings, a visible COMPLETE/PARTIAL status, and a per-provider cost breakdown.
**Mode:** mvp
**Depends on**: Phase 4
**Requirements**: MEMO-01, MEMO-02, MEMO-03, MEMO-04, MEMO-05, MEMO-06, OBS-02
**Success Criteria** (what must be TRUE):
  1. The memo renders six named collapsible sections: Fundamentals, Sentiment, Risk Register, Macro Context, Comparable Companies, and Synthesis.
  2. Every factual claim in the memo shows a citation reference; clicking it reveals the exact source excerpt inline without navigating away from the memo.
  3. The Contradictions section lists each agent disagreement with both claims shown side-by-side and a LOW, MEDIUM, or HIGH severity label.
  4. The memo header clearly displays COMPLETE or PARTIAL status so the user can calibrate trust before acting on the memo.
  5. The memo footer shows per-agent token counts (in/out), total cost in USD, and external API call counts per provider, aggregated to a memo-level total.
**Plans**: TBD
**UI hint**: yes

### Phase 6: Financial Metrics & Anomaly Detection
**Goal**: The FundamentalAnalysis agent stores typed financial metrics in a first-class persisted table with correct upsert semantics; isolation-forest anomaly detection surfaces named, severity-rated findings in the Fundamentals section derived from the company's own historical range.
**Mode:** mvp
**Depends on**: Phase 4, Phase 5
**Requirements**: METRIC-01, METRIC-02, METRIC-03
**Success Criteria** (what must be TRUE):
  1. After a research run, revenue, gross margin, and P/E (at minimum) appear in the FinancialMetric table keyed by (ticker, metric_name, period).
  2. Re-running research for the same ticker and period updates existing metric rows in place — no duplicate rows appear for the same (ticker, metric_name, period) key.
  3. At least one anomaly (when present) appears in the Fundamentals section as a named item with a LOW, MEDIUM, or HIGH severity rating rather than as prose.
  4. Anomaly thresholds are derived from the company's own historical range for that metric, not a fixed sector-wide average.
**Plans**: TBD
**UI hint**: yes

### Phase 7: Follow-Up Chat Session
**Goal**: After reading any completed memo, the user can open a persistent chat session grounded exclusively on the memo text and conversation history; sessions survive browser closes and re-logins; out-of-scope questions are escalated gracefully.
**Mode:** mvp
**Depends on**: Phase 5
**Requirements**: CHAT-01, CHAT-02, CHAT-03, CHAT-04
**Success Criteria** (what must be TRUE):
  1. A user can open a chat session on a completed memo and ask a follow-up question; the answer references specific memo content.
  2. Closing the browser, signing out, and returning to the memo reveals the full prior chat history with no messages lost.
  3. Follow-up answers never trigger a new RAG retrieval — they are grounded only on the memo text and the conversation history already in context.
  4. When a question clearly falls outside the memo's coverage, the system explicitly states this and prompts the user to submit a new research request.
**Plans**: TBD
**UI hint**: yes

### Phase 8: Watchlist Core & NEW_FILING Alert
**Goal**: Users can build a watchlist with optional investment theses, configure multiple independent alert rules per entry, and receive automatic research memos when SEC publishes new filings; alert history and pause/resume controls are available.
**Mode:** mvp
**Depends on**: Phase 4, Phase 5
**Requirements**: WATCH-01, WATCH-02, WATCH-03, WATCH-04, WATCH-09, WATCH-10
**Success Criteria** (what must be TRUE):
  1. A user can add a ticker to the watchlist and optionally attach an investment thesis string that alert-triggered memos will re-evaluate.
  2. Configuring a NEW_FILING alert rule on a watched ticker automatically triggers a new research memo run when SEC publishes a new 10-K, 10-Q, or 8-K.
  3. A user can configure multiple independent alert rules on the same watchlist entry and each fires independently.
  4. The alert history page lists every rule firing in chronological order, including SKIPPED events, with the full trigger payload visible.
  5. A user can pause an alert rule so it stops triggering memos, then resume it later — while paused no memos are spawned.
**Plans**: TBD
**UI hint**: yes

### Phase 9: Price & Scheduled Alerts, Lineage & RAGAS
**Goal**: Users can monitor price moves and set cron-scheduled research runs; every alert-triggered memo links to its immediate predecessor forming a navigable lineage chain; the RAGAS offline eval Celery beat job verifies retrieval quality against a curated golden set.
**Mode:** mvp
**Depends on**: Phase 8
**Requirements**: WATCH-05, WATCH-06, WATCH-07, WATCH-08, OBS-03
**Success Criteria** (what must be TRUE):
  1. Configuring a PRICE_MOVE alert with a 5% threshold fires a research memo run when the watched ticker moves more than 5% over the configured time window (verified end-to-end with a mocked yfinance response in tests).
  2. A SCHEDULED alert runs on its configured cron schedule and produces a memo; if no new documents have been ingested since the last memo, a WatchlistEvent with status SKIPPED is recorded and no memo is spawned.
  3. Each alert-triggered memo has a parent_memo_id linking to the most recent prior memo for the same ticker, and the UI allows navigating this lineage chain.
  4. The RAGAS offline eval Celery beat job runs automatically, tests against ≥ 20 curated query/expected-chunk pairs, and logs pass/fail results that indicate when retrieval quality regresses.
**Plans**: TBD
**UI hint**: yes

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation & Auth | TBD | Not started | - |
| 2. Document Ingestion Pipeline | TBD | Not started | - |
| 3. Research Request & Disambiguation | TBD | Not started | - |
| 4. Agent Execution & Live Progress | TBD | Not started | - |
| 5. ResearchMemo — Reading, Citations & Cost | TBD | Not started | - |
| 6. Financial Metrics & Anomaly Detection | TBD | Not started | - |
| 7. Follow-Up Chat Session | TBD | Not started | - |
| 8. Watchlist Core & NEW_FILING Alert | TBD | Not started | - |
| 9. Price & Scheduled Alerts, Lineage & RAGAS | TBD | Not started | - |
