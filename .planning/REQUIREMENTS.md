# Requirements: Vantage — Milestone v1.0 Walking Skeleton

**Defined:** 2026-06-27
**Milestone:** v1.0 Walking Skeleton
**Core Value:** Prove the full research pipeline works end-to-end — request → EDGAR ingest → hybrid RAG → 2 agents → ResearchMemo.

## v1 Requirements (Milestone Scope)

### Authentication

- [ ] **AUTH-01**: User can register an account with email and password
- [ ] **AUTH-02**: User can log in and remain authenticated across sessions via JWT
- [ ] **AUTH-03**: User can log out from any page

### Document Ingestion

- [ ] **INGEST-01**: SEC filings (10-K, 10-Q, 8-K) are automatically ingested when a ticker is researched; no manual upload required
- [ ] **INGEST-02**: Previously ingested public documents are reused across future research runs for the same ticker without re-fetching
- [ ] **INGEST-03**: Private user-uploaded documents are invisible to all other users
- [ ] **INGEST-04**: Ingestion failures for specific sources are surfaced to the user before the memo is read
- [ ] **INGEST-05**: The same public filing is deduplicated via canonical_id even when it arrives from multiple sources (e.g. EDGAR API + user upload)

### Research Request & Disambiguation

- [ ] **REQST-01**: User can submit a free-text research request (ticker symbol, investment thesis, or financial question)
- [ ] **REQST-02**: System auto-resolves unambiguous tickers (confidence ≥ 0.85) without prompting the user
- [ ] **REQST-03**: System returns a ClarificationResponse with candidate tickers when extraction confidence < 0.85; no ResearchMemo is created until resolved
- [ ] **REQST-04**: User can select the correct ticker from candidates and resubmit; resolved_tickers carries through automatically
- [ ] **REQST-05**: User can request multi-ticker research (e.g. "Compare AAPL and MSFT") in a single request
- [ ] **REQST-06**: User can upload a private PDF (e.g. proprietary financial model) as additional research input for a request

### Agent Execution (Minimal — 2 Agents)

- [ ] **EXEC-02**: Agent statuses are shown as SUCCESS, PARTIAL (output with gaps), or FAILED (no output)
- [ ] **EXEC-03**: System produces a PARTIAL ResearchMemo when FundamentalAnalysis fails, rather than aborting the run

### ResearchMemo Output (Minimal)

- [ ] **MEMO-01**: ResearchMemo is structured into named sections (Fundamentals and Synthesis at minimum; full section set in Milestone 2)
- [ ] **MEMO-02**: Every factual claim in the memo links to the specific source chunk it was derived from
- [ ] **MEMO-03**: Each source citation includes the quoted excerpt inline (no external doc required to understand the evidence)

## Future Requirements (Deferred to Milestone 2+)

### Agent Execution — Full Layer

- **EXEC-01**: Live agent progress panel via WebSocket (real-time per-agent status)
- **EXEC-04**: Failed/missing memo sections explicitly marked with reason (not silently omitted)
- **EXEC-05**: Research completes asynchronously; user can navigate away and return to completed memo

### ResearchMemo — Full Feature Set

- **MEMO-04**: Dedicated Contradictions section (severity-rated agent disagreements) — needs 3+ agents
- **MEMO-05**: COMPLETE / PARTIAL status displayed at a glance
- **MEMO-06**: Total generation cost shown (LLM tokens + external API call counts)

### Full Agent Suite (Milestone 2)

- **EXEC-full**: SentimentNLP, RiskAssessment, MacroSector, ComparableCompanies agents wired into LangGraph fan-out

### Follow-Up Chat / Sessions (Milestone 2)

- **CHAT-01**: Persistent follow-up chat session grounded on memo text
- **CHAT-02**: Follow-up answers grounded on memo + conversation history only (no RAG re-query)
- **CHAT-03**: Session persists indefinitely and is resumable
- **CHAT-04**: System flags when follow-up exceeds memo coverage

### Financial Metrics & Anomaly Detection (Milestone 2)

- **METRIC-01**: Structured financial metrics stored per (ticker, metric_name, period)
- **METRIC-02**: Anomalies appear as named, severity-rated items in fundamentals section
- **METRIC-03**: Anomaly detection uses company's own historical range

### Watchlist & Alerts (Milestone 2)

- **WATCH-01** through **WATCH-10**: All watchlist and alert rule features

### Observability & Cost (Milestone 2)

- **OBS-01**: LangSmith traces for every agent call
- **OBS-02**: Per-agent cost breakdown aggregated to per-memo total
- **OBS-03**: Offline RAGAS evaluation Celery beat job

## Out of Scope (Milestone 1)

| Feature | Reason |
|---------|--------|
| WebSocket progress panel | Adds async infrastructure complexity — walking skeleton is synchronous |
| Async Celery research tasks | Sync pipeline sufficient to prove end-to-end; async layer is Milestone 2 |
| 5 remaining specialist agents | Walking skeleton needs only FundamentalAnalysis + Synthesis |
| Contradictions panel | Requires 3+ agents with overlapping claims to be meaningful |
| Chat/session layer | Depends on completed memo pipeline — Milestone 2 |
| Watchlist & alerts | Depends on full async pipeline — Milestone 2 |
| Observability (LangSmith, RAGAS) | Quality gate for full system — Milestone 2 |
| Frontend beyond minimal API client | CLI / curl / Postman sufficient to prove the pipeline |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| AUTH-01 | Phase 1 | Pending |
| AUTH-02 | Phase 1 | Pending |
| AUTH-03 | Phase 1 | Pending |
| INGEST-01 | Phase 2 | Pending |
| INGEST-02 | Phase 2 | Pending |
| INGEST-03 | Phase 2 | Pending |
| INGEST-04 | Phase 2 | Pending |
| INGEST-05 | Phase 2 | Pending |
| REQST-01 | Phase 3 | Pending |
| REQST-02 | Phase 3 | Pending |
| REQST-03 | Phase 3 | Pending |
| REQST-04 | Phase 3 | Pending |
| REQST-05 | Phase 3 | Pending |
| REQST-06 | Phase 3 | Pending |
| EXEC-02 | Phase 4 | Pending |
| EXEC-03 | Phase 4 | Pending |
| MEMO-01 | Phase 4 | Pending |
| MEMO-02 | Phase 4 | Pending |
| MEMO-03 | Phase 4 | Pending |

**Coverage:**
- v1 requirements: 19 total
- Mapped to phases: 19/19 (100%)
- Unmapped: 0

---
*Requirements defined: 2026-06-27*
*Last updated: 2026-06-27 — traceability populated by roadmapper; 19/19 requirements mapped*
