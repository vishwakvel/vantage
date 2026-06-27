<!-- GSD:project-start source:PROJECT.md -->

## Project

**Vantage**

A multi-agent LangGraph platform that takes a free-text research request (ticker, investment thesis, or financial question) and produces a fully cited `ResearchMemo` in minutes via seven specialized AI agents backed by a multi-source RAG pipeline (SEC EDGAR, FRED, yfinance, NewsAPI, arXiv, user PDFs). Designed for individual investors and small teams who need institutional-quality research without the institutional budget. The standout differentiator: a structured Contradictions panel that explicitly surfaces where agents disagree — no silent conflict resolution.

**Core Value:** Given a ticker or investment thesis, produce a fully cited ResearchMemo with explicit Contradictions — in minutes, not the 8-12 hours a junior analyst needs.

### Constraints

- **Rate limit**: Groq free-tier ~6,000 tokens/min — shared async token-bucket rate limiter is day-one, non-negotiable; no code path calls Groq directly
- **EDGAR policy**: User-Agent header (`Vantage/1.0 vishwak.vel@gmail.com`) required on every EDGAR request — omission causes silent 429/403
- **Code boundary**: All external API calls live in `app/services/` only — never inline in agents
- **String constants**: Section string values defined in `app/ingestion/section_constants.py` only — never as inline literals
- **Test boundary**: No real API calls in tests — mock at `services/` boundary only
- **Git**: Feature branches only — never commit to main directly
- **Secrets**: Everything via `.env` (gitignored) — no hardcoded credentials

<!-- GSD:project-end -->

<!-- GSD:stack-start source:STACK.md -->

## Technology Stack

Technology stack not yet documented. Will populate after codebase mapping or first phase.
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->

## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->

## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->

## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->

## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:

- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->

<!-- GSD:profile-start -->

## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
