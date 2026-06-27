# Vantage

## What This Is

A multi-agent LangGraph platform that takes a free-text research request (ticker, investment thesis, or financial question) and produces a fully cited `ResearchMemo` in minutes via seven specialized AI agents backed by a multi-source RAG pipeline (SEC EDGAR, FRED, yfinance, NewsAPI, arXiv, user PDFs). Designed for individual investors and small teams who need institutional-quality research without the institutional budget. The standout differentiator: a structured Contradictions panel that explicitly surfaces where agents disagree — no silent conflict resolution.

## Core Value

Given a ticker or investment thesis, produce a fully cited ResearchMemo with explicit Contradictions — in minutes, not the 8-12 hours a junior analyst needs.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] User can submit a free-text research request and receive a fully cited ResearchMemo
- [ ] Disambiguation flow surfaces ambiguous tickers and blocks memo creation until resolved
- [ ] Seven specialist agents execute in parallel fan-out with graceful degradation to PARTIAL memo
- [ ] Synthesis agent produces a structured Contradictions list (severity-rated, not prose)
- [ ] Hybrid RAG pipeline (dense + BM25 + reranker) with per-claim source citations
- [ ] Persistent follow-up sessions grounded on memo text (no RAG re-query)
- [ ] Watchlist monitoring with NEW_FILING, PRICE_MOVE, and SCHEDULED alert rules
- [ ] Structured FinancialMetric storage enabling isolation-forest anomaly detection
- [ ] Full observability: LangSmith traces, per-agent cost breakdown, RAGAS offline eval

### Out of Scope

- SENTIMENT_SPIKE alert rule — requires continuous FinBERT inference on live news stream (v2)
- Cross-memo Anomaly/Risk queries — requires promoting Anomaly/Risk to first-class entity (v2)
- Admin roles / multi-role authorization — single user role at launch
- Email/push notifications for alerts — in-app pub/sub only at launch
- Social or sharing features — memos are private
- Real-time market data streaming — yfinance polling only
- Mobile native app — web frontend only
- Fine-tuning FinBERT — uses pre-trained model at launch

## Context

- **Tech stack**: Python 3.11, FastAPI, LangGraph, ChromaDB (single collection), PostgreSQL, Celery + Redis, React/TypeScript, Docker Compose
- **LLM**: Groq (Llama 3.1 70B) — free tier ~6,000 tokens/min; parallel agent execution exhausts this in seconds without rate limiting
- **Data sources**: All free — SEC EDGAR (full-text search API), FRED, yfinance, NewsAPI free tier, arXiv API, user PDF uploads
- **Agent architecture**: 7 agents — Orchestrator (routing, disambiguation), FundamentalAnalysis, SentimentNLP, RiskAssessment, MacroSector, ComparableCompanies (parallel fan-out), Synthesis (fan-in, owns memo status)
- **Prior work**: Full domain grilling session (40+ questions resolved), comprehensive PRD in docs/PRD.md, domain glossary in CONTEXT.md, 17 GitHub issues published

## Constraints

- **Rate limit**: Groq free-tier ~6,000 tokens/min — shared async token-bucket rate limiter is day-one, non-negotiable; no code path calls Groq directly
- **EDGAR policy**: User-Agent header (`Vantage/1.0 vishwak.vel@gmail.com`) required on every EDGAR request — omission causes silent 429/403
- **Code boundary**: All external API calls live in `app/services/` only — never inline in agents
- **String constants**: Section string values defined in `app/ingestion/section_constants.py` only — never as inline literals
- **Test boundary**: No real API calls in tests — mock at `services/` boundary only
- **Git**: Feature branches only — never commit to main directly
- **Secrets**: Everything via `.env` (gitignored) — no hardcoded credentials

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| ResearchMemo lifecycle: PENDING → RUNNING → COMPLETE \| PARTIAL \| FAILED | PARTIAL = memo exists with gaps (agent failures); FAILED = no usable memo (Orchestrator-level failure) | — Pending |
| Synthesis agent owns ResearchMemo.status | Only agent with full cross-agent visibility | — Pending |
| Declarative routing table (not LLM-driven) | Deterministic dispatch; LLM routing adds latency and failure surface for no benefit at this scale | — Pending |
| ClarificationResponse not persisted; no ResearchMemo until disambiguation resolved | Avoids orphaned memos in PENDING state forever | — Pending |
| Section strings are plain-string convention; constants live in `app/ingestion/section_constants.py` only | Prevents inline literals from drifting across codebase | — Pending |
| All Groq API calls go through a shared async rate limiter | Groq free-tier ~6,000 tokens/min; direct calls will 429 under parallel agent execution | — Pending |
| EDGAR client must include User-Agent on every request | EDGAR API policy; requests without User-Agent are blocked silently | — Pending |
| FinancialMetric is first-class persisted table, upsert key (ticker, metric_name, period) | Required for isolation-forest anomaly detection; can't run on raw AgentOutput fields | — Pending |
| Single Chroma collection (not per-ticker or per-user) | Metadata filtering handles scoping; multiple collections add operational complexity for no retrieval benefit | — Pending |
| RAGAS evaluation is offline-only (Celery beat job) | Per-memo online eval adds latency; offline golden set sufficient for retrieval quality signal | — Pending |
| Sessions ground follow-ups on memo text; no RAG re-query | Follow-ups are about the memo, not new research; re-querying pollutes context | — Pending |
| AgentOutput stored in its own table (not on AgentTask) | Outputs are large; storing on AgentTask row would bloat queries | — Pending |
| Company entity (ticker PK) normalizes ticker strings across all tables | No raw ticker strings in FKs; avoids case/exchange prefix drift | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-06-27 after initialization*
