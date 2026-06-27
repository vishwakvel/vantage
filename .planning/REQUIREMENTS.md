# Requirements: Vantage

**Defined:** 2026-06-27
**Core Value:** Given a ticker or investment thesis, produce a fully cited ResearchMemo with explicit Contradictions — in minutes, not hours.

## v1 Requirements

### Authentication

- [ ] **AUTH-01**: User can register an account with email and password
- [ ] **AUTH-02**: User can log in and remain authenticated across sessions via JWT
- [ ] **AUTH-03**: User can log out from any page

### Research Request & Disambiguation

- [ ] **REQST-01**: User can submit a free-text research request (ticker symbol, investment thesis, or financial question)
- [ ] **REQST-02**: System auto-resolves unambiguous tickers (confidence ≥ 0.85) without prompting the user
- [ ] **REQST-03**: System returns a ClarificationResponse with candidate tickers when extraction confidence < 0.85; no ResearchMemo is created until resolved
- [ ] **REQST-04**: User can select the correct ticker from candidates and resubmit; resolved_tickers carries through automatically
- [ ] **REQST-05**: User can request multi-ticker research (e.g. "Compare AAPL and MSFT") in a single request
- [ ] **REQST-06**: User can upload a private PDF (e.g. proprietary financial model) as additional research input for a request

### Research Execution & Progress

- [ ] **EXEC-01**: User sees a live agent progress panel showing each agent's status while a ResearchMemo is being generated
- [ ] **EXEC-02**: Agent statuses are shown as SUCCESS, PARTIAL (output with gaps), or FAILED (no output)
- [ ] **EXEC-03**: System produces a PARTIAL ResearchMemo when one or more agents fail, rather than failing the entire run
- [ ] **EXEC-04**: Failed or missing memo sections are explicitly marked with a reason, not silently omitted
- [ ] **EXEC-05**: Research completes asynchronously; user can navigate away and return to a completed memo

### ResearchMemo — Reading & Navigation

- [ ] **MEMO-01**: ResearchMemo is structured into named, collapsible sections: Fundamentals, Sentiment, Risk Register, Macro Context, Comparable Companies, Synthesis
- [ ] **MEMO-02**: Every factual claim in the memo links to the specific source chunk it was derived from
- [ ] **MEMO-03**: Each source citation includes the quoted excerpt inline (no external doc required to understand the evidence)
- [ ] **MEMO-04**: ResearchMemo has a dedicated Contradictions section listing agent disagreements with both claims shown and a severity rating (LOW / MEDIUM / HIGH)
- [ ] **MEMO-05**: ResearchMemo displays COMPLETE or PARTIAL status at a glance so users can calibrate trust before acting
- [ ] **MEMO-06**: ResearchMemo shows the total generation cost: LLM tokens (in/out) and external API call count

### Follow-Up Chat (Session)

- [ ] **CHAT-01**: User can ask follow-up questions about a memo in a persistent chat session
- [ ] **CHAT-02**: Follow-up answers are grounded exclusively on the memo text and conversation history — the RAG pipeline is not re-queried
- [ ] **CHAT-03**: Chat session persists indefinitely and is resumable across browser closes and sign-outs
- [ ] **CHAT-04**: System indicates when a follow-up question genuinely exceeds the memo's coverage, prompting a new research request

### Document Ingestion

- [ ] **INGEST-01**: SEC filings (10-K, 10-Q, 8-K) are automatically ingested when a ticker is researched; no manual upload required
- [ ] **INGEST-02**: Previously ingested public documents are reused across future research runs for the same ticker without re-fetching
- [ ] **INGEST-03**: Private user-uploaded documents are invisible to all other users
- [ ] **INGEST-04**: Ingestion failures for specific sources are surfaced to the user before the memo is read
- [ ] **INGEST-05**: The same public filing is deduplicated via canonical_id even when it arrives from multiple sources (e.g. EDGAR API + user upload)

### Financial Metrics & Anomaly Detection

- [ ] **METRIC-01**: Structured financial metrics (revenue, gross margin, P/E, etc.) are extracted and stored per (ticker, metric_name, period) as a first-class persisted table; newer filings supersede older values for the same period
- [ ] **METRIC-02**: Anomalies in the fundamentals section appear as named, severity-rated items (e.g. "gross margin compression outside historical range for this sector")
- [ ] **METRIC-03**: Anomaly detection compares a metric against the company's own historical range, not a generic sector threshold

### Watchlist & Alerts

- [ ] **WATCH-01**: User can add a ticker to their watchlist to enable ongoing monitoring
- [ ] **WATCH-02**: User can attach an optional investment thesis to a watchlist entry that alert-triggered memos will re-evaluate
- [ ] **WATCH-03**: User can configure multiple independent AlertRules on a single watchlist entry
- [ ] **WATCH-04**: NEW_FILING alert rule fires when SEC publishes a new 10-K, 10-Q, or 8-K for a watched ticker
- [ ] **WATCH-05**: PRICE_MOVE alert rule fires when a watched ticker moves beyond a user-configurable percentage over a user-configurable time window
- [ ] **WATCH-06**: SCHEDULED alert rule re-runs research on a user-configured cron schedule
- [ ] **WATCH-07**: SCHEDULED alerts skip spawning a memo when no new Documents have been ingested for the ticker since the last memo; a WatchlistEvent with status SKIPPED is still recorded
- [ ] **WATCH-08**: Alert-spawned ResearchMemos link to the most recent prior memo for the same ticker via parent_memo_id, forming a lineage chain
- [ ] **WATCH-09**: User can view a history of every AlertRule firing, including SKIPPED runs, with the trigger payload that caused it
- [ ] **WATCH-10**: User can pause and resume individual AlertRules without deleting them

### Observability & Cost

- [ ] **OBS-01**: Every LangGraph agent call is traced in LangSmith with full inputs and outputs
- [ ] **OBS-02**: Per-agent cost breakdown (tokens_in, tokens_out, cost_usd, external_api_calls per provider) is aggregated to a per-memo total
- [ ] **OBS-03**: A periodic offline RAGAS evaluation Celery beat job runs against a golden test set (target: ≥ 20 curated query/expected-chunks pairs) to catch retrieval regressions

## v2 Requirements

### Advanced Alerts

- **ALERT-V2-01**: SENTIMENT_SPIKE alert rule that fires on significant sentiment change detected via continuous FinBERT inference on live news stream

### Cross-Memo Analytics

- **CROSS-V2-01**: Cross-memo Anomaly queries ("show all anomalies for AAPL over time") — requires promoting Anomaly to first-class entity
- **CROSS-V2-02**: Cross-memo Risk queries across memo lineage chains — same prerequisite

### Notifications

- **NOTF-V2-01**: Email or push notifications when alert rules fire (in-app pub/sub only at launch)

### Social & Sharing

- **SOCIAL-V2-01**: Memo sharing features (memos are private to owning user at launch)

### Mobile

- **MOBILE-V2-01**: Native mobile app (web frontend only at launch)

### ML

- **ML-V2-01**: Fine-tuned FinBERT for sentiment (uses pre-trained model at launch)

## Out of Scope

| Feature | Reason |
|---------|--------|
| SENTIMENT_SPIKE alert rule | Requires continuous FinBERT inference on live news stream — v2 |
| Cross-memo Anomaly/Risk analytics | Requires promoting Anomaly and Risk to first-class entities — v2 |
| Admin roles / multi-role authorization | Single user role at launch; RBAC is v2 |
| Email/push alert notifications | In-app pub/sub only; email adds deliverability ops overhead |
| Memo sharing / social features | Memos are scoped to owning user; sharing is v2 |
| Real-time market data streaming | Price polling via yfinance only; streaming adds WebSocket + feed cost |
| Mobile native app | Web-first; React PWA if needed |
| Fine-tuning FinBERT | Pre-trained model sufficient at launch |
| Real API calls in tests | Mock at services/ boundary — never in tests |
| Committing to main directly | Feature branches only per CLAUDE.md |

## Traceability

Populated by roadmapper agent — 2026-06-27.

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
| EXEC-01 | Phase 4 | Pending |
| EXEC-02 | Phase 4 | Pending |
| EXEC-03 | Phase 4 | Pending |
| EXEC-04 | Phase 4 | Pending |
| EXEC-05 | Phase 4 | Pending |
| OBS-01 | Phase 4 | Pending |
| MEMO-01 | Phase 5 | Pending |
| MEMO-02 | Phase 5 | Pending |
| MEMO-03 | Phase 5 | Pending |
| MEMO-04 | Phase 5 | Pending |
| MEMO-05 | Phase 5 | Pending |
| MEMO-06 | Phase 5 | Pending |
| OBS-02 | Phase 5 | Pending |
| METRIC-01 | Phase 6 | Pending |
| METRIC-02 | Phase 6 | Pending |
| METRIC-03 | Phase 6 | Pending |
| CHAT-01 | Phase 7 | Pending |
| CHAT-02 | Phase 7 | Pending |
| CHAT-03 | Phase 7 | Pending |
| CHAT-04 | Phase 7 | Pending |
| WATCH-01 | Phase 8 | Pending |
| WATCH-02 | Phase 8 | Pending |
| WATCH-03 | Phase 8 | Pending |
| WATCH-04 | Phase 8 | Pending |
| WATCH-09 | Phase 8 | Pending |
| WATCH-10 | Phase 8 | Pending |
| WATCH-05 | Phase 9 | Pending |
| WATCH-06 | Phase 9 | Pending |
| WATCH-07 | Phase 9 | Pending |
| WATCH-08 | Phase 9 | Pending |
| OBS-03 | Phase 9 | Pending |

**Coverage:**
- v1 requirements: 45 total
- Mapped to phases: 45/45
- Unmapped: 0

---
*Requirements defined: 2026-06-27*
*Last updated: 2026-06-27 — traceability populated by roadmapper; 9 phases, 100% coverage*
