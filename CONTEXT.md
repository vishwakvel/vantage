# Vantage Domain Glossary

## Core Artifacts

**ResearchRequest** — The raw user input to the system. Free text. The Orchestrator classifies its intent and extracts subjects. Carries a `resolved_tickers` field on resubmission after disambiguation. Never persisted until disambiguation is resolved.

**ResearchPlan** — The Orchestrator's internal execution plan derived from a `ResearchRequest`. Persisted. Tracks which agents will run, their inputs, and execution phases (INGESTION → AGENT_EXECUTION → SYNTHESIS). Has two independent status fields: `status` and `ingestion_status`.

**ResearchMemo** — The primary output artifact. A structured, cited investment research document produced by the Synthesis agent. Has explicit lifecycle states: PENDING, RUNNING, COMPLETE, PARTIAL, FAILED. May carry a `parent_memo_id` linking it to a prior memo for the same subject (lineage chain). Soft-deleted only, never hard-deleted.

**ClarificationResponse** — A pre-memo API response returned when ticker extraction confidence falls below the auto-pick threshold. Not persisted. Contains ambiguous terms and candidate tickers for the user to resolve. No `ResearchMemo` is created until clarification is resolved.

## Agent Layer

**AgentTask** — A unit of work delegated to a specialist agent within a `ResearchPlan`. Tracks agent type, status, inputs, and a reference to its `AgentOutput`. Status: PENDING, RUNNING, SUCCESS, PARTIAL, FAILED. PARTIAL means output was returned but with gaps (`missing_fields`). FAILED means no output at all.

**AgentOutput** — The typed, structured result of an `AgentTask`. Stored separately from `AgentTask` (can be large). Every agent returns an output — agents never raise unhandled exceptions. Carries `completeness: FULL | PARTIAL` and `missing_fields`.

**Orchestrator** — The coordinating agent. Classifies `ResearchRequest` intent, resolves tickers, builds the `ResearchPlan`, and dispatches `AgentTask`s. Uses a declarative routing table (not an LLM) to determine which specialist agents run. Does not perform research itself.

**Synthesis** — The final specialist agent. Gates on completion of all other agents. Aggregates `AgentOutput`s into a `ResearchMemo`. Owns `ResearchMemo.status` and `Contradiction.severity`. The only agent with full cross-agent visibility.

## RAG Layer

**Document** — The canonical ingestion unit. One filing, article, PDF, or data series snapshot. Has a `canonical_id` (deterministic hash) for deduplication. `visibility: PUBLIC | PRIVATE`; private documents are user-scoped. Public documents (EDGAR filings, news, FRED) are global and reusable across all requests.

**Chunk** — A sub-unit of a `Document`, produced during ingestion. The unit of retrieval. `section` is a plain string enforced by convention in the ingestion layer (e.g. "risk_factors", "mda", "speaker_turn_14").

**Source** — A chunk-level citation attached to a claim in a `ResearchMemo`. References a specific `Document` and `Chunk` with a relevance score and quoted excerpt.

## Structured Outputs

**FinancialMetric** — A first-class persisted entity. One row per (ticker, metric_name, period). Upserted when newer data supersedes older. Provides the clean time-series required for anomaly detection.

**Contradiction** — A structured object on `ResearchMemo` (top-level list, not embedded in prose). Records two conflicting agent claims, the agents that made them, and a severity (LOW, MEDIUM, HIGH). Severity owned by Synthesis.

**Anomaly** — A flagged outlier in financial fundamentals detected by the isolation forest. Nested inside `FundamentalAnalysisOutput`. Not a first-class entity at launch.

**Risk** — A single entry in the risk register produced by `RiskAssessment`. Nested inside `RiskAssessmentOutput`. Not a first-class entity at launch.

## User & Session Layer

**User** — A tenant. Owns `ResearchMemo`s, `Session`s, `Watchlist`s, and private `Document`s.

**Session** — A conversation context scoped to one `ResearchMemo`. Persists indefinitely (resumable). Grounds follow-up questions on the memo text and conversation history. A new `ResearchRequest` starts a new `Session`; follow-ups do not re-query the RAG pipeline.

**Message** — A single turn in a `Session`. Append-only (no edits, no deletes). Role: USER or ASSISTANT.

## Watchlist Layer

**Watchlist** — A user's standing intent to monitor a ticker, with an optional thesis to re-evaluate against.

**AlertRule** — A trigger condition on a `Watchlist`. One `Watchlist` has many `AlertRule`s. Trigger types: NEW_FILING, PRICE_MOVE, SCHEDULED. Configuration is a typed dict (stable schema for future trigger types). SCHEDULED rules only spawn a `ResearchMemo` when new `Document`s exist for the ticker since the last memo.

**WatchlistEvent** — A persisted record of an `AlertRule` firing. Links to the `AlertRule` that fired, the trigger payload (exact data that caused the trigger), and the spawned `ResearchMemo` (nullable — null if the run was SKIPPED because no new data existed).
