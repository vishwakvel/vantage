# PRD: Vantage — Multi-Agent Financial Research Platform

## Problem Statement

Conducting thorough investment research on a public company requires synthesizing information across SEC filings, earnings call transcripts, macroeconomic indicators, news, and peer comparisons. Done properly, this takes a junior analyst 8–12 hours per company: locating and reading 10-Ks, extracting financial metrics, flagging anomalies, assessing risk factors, building a peer comp table, and distilling it all into a structured memo. The process is expensive, slow, and inaccessible to individual investors and small teams without institutional research budgets.

There is no existing tool that (a) automates the full research pipeline across multiple authoritative data sources, (b) produces a structured, cited memo at institutional quality, and (c) explicitly surfaces contradictions when data sources disagree rather than hiding them in a confident-sounding summary.

## Solution

Vantage accepts a free-text `ResearchRequest` — a stock ticker, investment thesis, or financial question — and produces a fully cited `ResearchMemo` in minutes. Seven specialized AI agents orchestrated via LangGraph each own a distinct research domain: fundamental analysis, sentiment from earnings transcripts, risk factor assessment, macroeconomic context, comparable company valuation, and final synthesis. The Synthesis agent explicitly identifies contradictions between agents rather than silently resolving them, giving the user a transparent view of where the evidence is in conflict.

A multi-source RAG pipeline ingests SEC EDGAR filings, FRED macro data, financial metrics, news, academic papers, and user-uploaded PDFs into a hybrid retrieval system (dense vector + BM25 + cross-encoder reranking). Every claim in the memo is linked to a specific source chunk. Users can ask follow-up questions in a persistent session grounded on the memo. A watchlist system monitors tracked tickers and automatically spawns new `ResearchMemo`s when new filings drop, prices move significantly, or on a recurring schedule.

## User Stories

### Research Request & Disambiguation

1. As an investor, I want to submit a free-text research request (ticker, thesis, or question) so that I can start research without learning a structured query language.
2. As an investor, I want the system to automatically resolve obvious tickers (e.g. "AAPL") without interrupting me, so that common cases are frictionless.
3. As an investor, I want the system to ask me to clarify ambiguous inputs (e.g. "Meta" or "Apple") with a list of candidates, so that research is never run against the wrong company.
4. As an investor, I want to combine multiple input types in one request (e.g. "Compare AAPL and MSFT on cloud margins"), so that multi-ticker synthesis is a single action, not multiple separate lookups.
5. As an investor, I want to upload a proprietary PDF (e.g. my own financial model or internal report) as part of a research request, so that the system can incorporate my private data alongside public sources.
6. As an investor submitting a clarification response, I want my resolved ticker to carry through to the research run automatically, so that I don't have to re-type my original question.

### Research Execution & Progress

7. As an investor, I want to see a live agent progress panel while my `ResearchMemo` is being generated, so that I understand what the system is doing and roughly how long it will take.
8. As an investor, I want to see clearly when an agent completes successfully, partially, or fails entirely, so that I can evaluate how complete the resulting memo will be before reading it.
9. As an investor, I want the system to proceed and produce a memo even when one or more specialist agents fail or return partial results, so that a single API timeout doesn't invalidate the whole research run.
10. As an investor, I want failed or missing memo sections to be clearly marked with a reason, rather than silently omitted, so that I know what research gaps exist.
11. As an investor, I want research to complete asynchronously in the background, so that I can navigate away and return to a completed memo.

### ResearchMemo — Reading & Navigation

12. As an investor, I want to read a structured memo with named, collapsible sections (Fundamentals, Sentiment, Risk Register, Macro Context, Comparable Companies, Synthesis), so that I can navigate directly to the section most relevant to my decision.
13. As an investor, I want every factual claim in the memo to be linked to the specific source chunk it came from, so that I can verify the underlying evidence without manual searching.
14. As an investor, I want to see the quoted excerpt from each source inline with the citation, so that I don't have to open the source document to understand the evidence.
15. As an investor, I want a dedicated Contradictions section that explicitly lists cases where agents disagreed, with both claims shown and a severity rating, so that I can evaluate conflicting evidence directly rather than receiving a falsely confident synthesis.
16. As an investor, I want to see whether a `ResearchMemo` is complete or partial at a glance, so that I can calibrate my trust in the output before acting on it.
17. As an investor, I want to see the total cost of generating a memo (LLM tokens + API calls), so that I can understand the resource cost of each research run.

### Follow-Up Chat (Session)

18. As an investor, I want to ask follow-up questions about a memo in a persistent chat session, so that I can drill into specific claims without re-running the full research pipeline.
19. As an investor, I want follow-up answers to be grounded on the memo I just read, so that the assistant doesn't hallucinate outside the researched evidence base.
20. As an investor, I want my chat session to persist indefinitely so that I can return to a research conversation the next day and continue without losing context.
21. As an investor, I want the system to tell me when a follow-up question genuinely goes beyond the memo's coverage, so that I know when to start a new research request rather than expect an answer from the existing session.

### Document Ingestion

22. As an investor, I want SEC filings (10-K, 10-Q, 8-K) to be automatically ingested when I research a ticker, so that I don't need to manually locate and upload filings.
23. As an investor, I want recently ingested documents to be available to all my future research requests for the same ticker, so that I don't pay re-ingestion cost on subsequent runs.
24. As an investor, I want my private uploaded documents to be invisible to other users, so that my proprietary research materials remain confidential.
25. As an investor, I want to be informed if document ingestion fails for a specific source, so that I know which data gaps exist before reading the memo.
26. As a developer, I want the same public filing to be deduplicated automatically even if it arrives from multiple sources (e.g. EDGAR API and a user upload), so that the vector store doesn't contain redundant chunks that inflate retrieval scores.

### Financial Metrics & Anomaly Detection

27. As an investor, I want structured financial metrics (revenue, gross margin, P/E, etc.) extracted and stored per fiscal period, so that the system can run trend analysis across quarters without re-fetching raw filings.
28. As an investor, I want flagged anomalies in the fundamentals section (e.g. "gross margin compression outside historical range for this sector") to appear as named, severity-rated items, so that I can immediately identify what the system considers unusual.
29. As an investor, I want anomaly detection to compare a metric against the company's own historical range, not just a generic threshold, so that flags are company-context-aware.

### Watchlist & Alerts

30. As an investor, I want to add a ticker to my watchlist, so that I can track it for ongoing monitoring without re-submitting research requests manually.
31. As an investor, I want to attach an optional thesis to a watchlist entry (e.g. "thesis: NVDA will dominate AI inference"), so that alert-triggered research runs re-evaluate that specific thesis against new data.
32. As an investor, I want to configure multiple independent alert rules on a single watchlist entry, so that I can be notified on new filings AND significant price moves without creating duplicate watchlist entries.
33. As an investor, I want a `NEW_FILING` alert rule that fires when SEC publishes a new 10-K, 10-Q, or 8-K for a watched ticker, so that I receive research automatically when material disclosures occur.
34. As an investor, I want a `PRICE_MOVE` alert rule that fires when a watched ticker moves more than a configurable percentage over a configurable time window, so that I receive research automatically on significant price action.
35. As an investor, I want a `SCHEDULED` alert rule that re-runs research on a cron schedule, so that I receive periodic research refreshes for tickers I track long-term.
36. As an investor, I want scheduled alerts to skip re-running if no new data has been ingested for the ticker since the last memo, so that I don't receive duplicate memos with no new information.
37. As an investor, I want alert-spawned `ResearchMemo`s to link back to the prior memo for the same ticker, so that I can see the lineage of research over time.
38. As an investor, I want to see a history of every time an alert rule fired (including skipped runs), with the trigger payload that caused it, so that I can debug whether my alert logic is working as intended.
39. As an investor, I want to pause and resume individual alert rules without deleting them, so that I can temporarily mute alerts during periods when I'm not actively monitoring.

### Auth & Account

40. As a user, I want to register and log in with email and password, so that my memos, sessions, and watchlist are private to my account.
41. As a user, I want my JWT session to persist long enough that I don't need to re-authenticate mid-research, so that background async research jobs complete under my authenticated context.

### Observability & Cost

42. As a developer, I want every LangGraph agent call traced in LangSmith, so that I can inspect exactly what each agent received and returned on any research run.
43. As a developer, I want per-agent cost breakdowns (tokens in/out, USD cost, external API call count) aggregated to a per-memo total, so that I can identify which agents are expensive and optimize them.
44. As a developer, I want retrieval quality measured by a periodic RAGAS evaluation pipeline against a golden test set, so that regressions in chunking, embedding, or reranking are caught before they degrade memo quality.

## Implementation Decisions

### Domain Model

The core artifact is the **`ResearchMemo`** — a structured, cited investment research document. It has five terminal states: `PENDING`, `RUNNING`, `COMPLETE`, `PARTIAL`, `FAILED`. `PARTIAL` means the memo was produced but with one or more missing sections due to agent failure. `FAILED` means no memo was produced (Orchestrator-level failure). The `Synthesis` agent owns `ResearchMemo.status`.

A **`ResearchPlan`** is created from each `ResearchRequest` and persisted independently. It has two status fields: `status` (overall execution) and `ingestion_status` (document ingestion phase). Execution phases are strictly ordered: `INGESTION → AGENT_EXECUTION → SYNTHESIS`. Agents cannot dispatch until `ingestion_status` reaches `SUCCESS` or `PARTIAL`. If ingestion fully fails, the plan fails fast rather than agents querying an empty vector store.

A **`Company`** entity (`ticker PK, name, exchange, sector`) normalizes ticker strings across all tables. All entities that reference a ticker do so via FK to `Company`, never as a raw string.

**`FinancialMetric`** is a first-class persisted entity (not JSON in `AgentOutput`) because the isolation forest anomaly detector requires a clean time-series of rows. Upsert key: `(ticker, metric_name, period)`. A newer filing supersedes an older value for the same period.

**`Contradiction`** is a top-level structured list on `ResearchMemo` (not embedded in prose), enabling the frontend to render a dedicated contradiction panel and enabling cross-memo contradiction analysis. `Synthesis` owns `severity`.

**`Document`** has a `canonical_id` (deterministic hash of source + doc_type + ticker + filing_date + accession number/URL) for deduplication. Public filings (`visibility: PUBLIC`, `owner_id: null`) are shared across all users. Private uploads (`visibility: PRIVATE`, `owner_id: UUID`) are invisible to other users.

### Agent Architecture

Agents 2–6 (FundamentalAnalysis, SentimentNLP, RiskAssessment, MacroSector, ComparableCompanies) execute in parallel as a LangGraph fan-out. Synthesis gates on their completion (fan-in). The Orchestrator does not re-run failed agents — Synthesis proceeds with whatever succeeded and marks missing sections.

Agent routing is a **declarative table**, not LLM-driven, for auditability and testability:

| Agent | Requires |
|---|---|
| FundamentalAnalysis | `TICKER` in intent set |
| SentimentNLP | `TICKER` in intent set |
| RiskAssessment | `TICKER` in intent set |
| ComparableCompanies | `TICKER` in intent set |
| MacroSector | always |
| Synthesis | always |

Agents never raise unhandled exceptions. Every agent returns a typed `AgentOutput` with `completeness: FULL | PARTIAL` and `missing_fields: list[str]`. `AgentTask.status` is `PARTIAL` (not `FAILED`) when an agent returns output with gaps; `FAILED` only when an agent returns nothing.

`AgentOutput` is stored in its own table (not on `AgentTask`) because outputs are large and would bloat the task row.

### Disambiguation Flow

When the Orchestrator extracts a ticker with confidence ≥ 0.85, it proceeds automatically. Below that threshold, it returns a `ClarificationResponse` at the API layer — no `ResearchMemo` is created. The user resolves the ambiguity and resubmits with `resolved_tickers` set. This keeps the database free of orphaned half-created memos.

### RAG Pipeline

The retrieval service is **shared** (not per-agent). Each agent queries it with its own `ChunkFilters` (ticker, doc_type, section, filing_date range). Single Chroma collection. Hybrid retrieval: dense (Chroma) + sparse (BM25), scores merged via reciprocal rank fusion, cross-encoder reranker on top-k results.

`Chunk.section` is a plain string enforced by convention in the ingestion layer per `doc_type` (not typed at the schema level). All section string values are defined in a single constants module — never as inline literals elsewhere in the codebase.

### Session & Follow-Up

A `Session` is scoped to one `ResearchMemo` and persists indefinitely. Follow-up LLM calls are grounded on the memo text + conversation history only. The RAG pipeline is not re-queried for follow-ups. If a follow-up genuinely exceeds the memo's coverage, the correct path is a new `ResearchRequest`. `Message` records are append-only.

### Watchlist & Alert Model

`Watchlist` has many `AlertRule`s. Each `AlertRule` carries a `config` dict (not typed columns) so new trigger types (e.g. `SENTIMENT_SPIKE`) can be added without schema migrations. `SCHEDULED` rules only spawn a `ResearchMemo` when new `Document`s have been ingested for the ticker since the last memo; otherwise a `WatchlistEvent` is recorded with `spawned_memo_id: null` and status `SKIPPED`. Alert-spawned memos set `parent_memo_id` to the most recent prior memo for the same ticker.

### Infrastructure Constraints

All Groq API calls go through a shared async rate limiter (token bucket). This is a day-one architectural constraint, not an optimization — Groq free-tier rate limits will saturate immediately with six agents running in parallel. No code path calls Groq directly.

The EDGAR ingestion client must include a valid `User-Agent` header on every request (SEC policy). Omitting it results in silent 429/403 failures with no useful error messages.

### WebSocket Event Model

The backend publishes typed events over a WebSocket channel per `ResearchMemo`:

```
PLAN_CREATED → INGESTION_STARTED → INGESTION_COMPLETE
  → AGENT_STARTED      { agent_type, agent_task_id }
  → AGENT_COMPLETE     { agent_type, status: SUCCESS }
  → AGENT_PARTIAL      { agent_type, missing_fields: [...] }
  → SYNTHESIS_STARTED
  → MEMO_READY         { memo_id, status: COMPLETE | PARTIAL }
```

`AGENT_PARTIAL` is a distinct event from `AGENT_COMPLETE` so the frontend can visually differentiate a successful agent from a degraded one.

### Cost Tracking

Cost is tracked at the `AgentTask` level: `llm_tokens_in`, `llm_tokens_out`, `llm_cost_usd`, `external_api_calls: dict[provider, count]`. The `ResearchMemo` aggregates these into a total. External API calls are counted even when free-tier (for rate limit monitoring).

### RAGAS Evaluation

RAGAS runs offline as a periodic Celery beat job against a golden test set (target: ≥ 20 hand-curated `(query, expected_source_chunks)` pairs). Not per-request — per-request RAGAS adds latency and burns tokens on every memo. The evaluation pipeline catches regressions in chunking strategy, embedding model, or reranker configuration.

## Testing Decisions

### What makes a good test

Tests assert on externally observable behavior at a defined seam, not on internal implementation details. A test should fail when the system does the wrong thing, and pass when it does the right thing, regardless of which internal modules are involved. Mock only at the `services/` boundary (external API calls) — never mock internal logic.

### Seam 1 (primary): LangGraph graph invocation

`invoke_research_graph(state: ResearchState) -> ResearchState`

This is the highest and most valuable seam. A single call covers Orchestrator routing, agent fan-out/fan-in, PARTIAL degradation, Contradiction detection, and final memo assembly — without HTTP or Celery. External service calls (Groq, EDGAR, yfinance, NewsAPI) are mocked at the `services/` boundary. Tests at this seam validate:
- Correct agent routing for each intent combination
- Fan-out executes all required agents, skips irrelevant ones
- A failing agent produces a PARTIAL memo with the correct gap noted, not a FAILED run
- Contradictions between agent outputs are detected and appear in the top-level list
- `ResearchMemo.status` is set by Synthesis based on which sections are present

This seam is the primary regression guard for every agent change.

### Seam 2: Retrieval function

`retrieve(query: str, filters: ChunkFilters) -> list[RankedChunk]`

Tests hybrid retrieval in isolation: dense + BM25 score merging, reranker ordering, metadata filter application. Uses a small in-process Chroma instance with fixture chunks rather than mocking. Validates:
- Metadata filters correctly narrow results by ticker, doc_type, section
- Cross-encoder reranker reorders results vs. first-pass ranking
- BM25 contributes to scores for keyword-heavy queries

### Seam 3: Ingestion function

`ingest_document(doc: Document) -> list[Chunk]`

Tests chunking and metadata tagging per `doc_type`. Validates:
- Earnings calls chunk by speaker turn
- 10-Ks chunk by section, section strings match `section_constants`
- News chunks by paragraph
- `canonical_id` prevents duplicate ingestion of the same document
- Private documents are not returned by queries from other users

## Out of Scope

- `SENTIMENT_SPIKE` alert rule (requires continuous FinBERT inference on live news stream — v2)
- Cross-memo Anomaly queries ("show all anomalies for AAPL over time" — v2, requires promoting Anomaly to first-class entity)
- Cross-memo Risk queries (same reason — v2)
- Admin roles or multi-role authorization (single user role at launch)
- Email or push notifications for alerts (pub/sub in-app only at launch)
- Social or sharing features (memos are private to the owning user)
- Real-time market data streaming (price polling via yfinance only)
- Mobile native app (web frontend only)
- Fine-tuning FinBERT (uses pre-trained model at launch)

## Further Notes

**Groq rate limits are the primary operational risk.** Groq free-tier caps at ~6,000 tokens/min for Llama 3.1 70B. With six agents in parallel, a single research run can exhaust this in seconds. The shared async rate limiter is non-negotiable infrastructure from day one. Load-test the rate limiter explicitly in Milestone 6 before any demo.

**The Contradiction panel is the standout differentiator.** No competing research tool surfaces agent disagreements as a structured, severity-rated list. This is the feature most worth showing in portfolio and interview contexts. Prioritize its frontend implementation and ensure the Synthesis agent prompt is explicitly instructed to populate it rather than silently resolving conflicts.

**Memo lineage compounds in value over time.** Alert-spawned memos with `parent_memo_id` chains create a research history for each ticker. This becomes increasingly valuable as the watchlist grows — users can see how a company's research profile evolved across quarters. The data model supports this from day one; the UI for browsing lineage chains is a natural v2 feature.

**EDGAR `User-Agent` is a hard operational requirement**, not a best practice. Requests without a valid `User-Agent` identifying the application and a contact email will be rate-limited or blocked by SEC without meaningful error messages. This must be tested explicitly (verify the header is present in outbound requests, not just assumed).
