# Vantage — Multi-Agent Financial Research Platform
Multi-agent LangGraph system producing institutional-quality 
investment research memos from multi-source RAG pipelines.
Python 3.11. FastAPI backend. React frontend.

## Commands
- Start: `docker-compose up`
- Test: `pytest tests/ -v --cov=app`
- Lint: `ruff check . && black --check .`
- Type check: `pyrefly check`
- Frontend: `cd frontend && npm run dev`

## Architecture
- `app/agents/` — LangGraph agent nodes, one file per agent
- `app/rag/` — ingestion, chunking, retrieval, reranking
- `app/api/` — FastAPI routes under /api/v1/
- `app/models/` — Pydantic schemas for all agent I/O
- `app/services/` — external API clients only
- `app/workers/` — async Celery tasks
- `app/db/` — SQLAlchemy models and migrations
- `app/graph/` — LangGraph state and graph construction
- `frontend/` — React + TypeScript
- `tests/` — mirrors app/ structure
- `../vantage-notes/` — Obsidian vault, NOT in this repo

## Data Sources (all free)
- SEC EDGAR full-text search API
- FRED API for macro indicators
- yfinance for structured financial metrics
- NewsAPI free tier
- arXiv API for academic finance papers
- User PDF uploads

## Hard Rules
- Every agent output is a typed Pydantic model. No raw strings.
- All external API calls live in app/services/ only.
- No real API calls in tests. Mock everything external.
- Never commit secrets. Everything via .env (gitignored).
- Feature branches only. Never commit to main directly.
- Conventional commits: feat:, fix:, refactor:, test:, docs:, chore:
- Read files before making claims about what they do.
- Tests alongside implementation, never after.
- Every agent needs: implementation + unit test + integration test.
- Agents must degrade gracefully — failed sub-agent = noted gap in memo.
- Run tests automatically after implementation. Fix before moving on.
- Do not ask permission to lint, typecheck, or run tests.
- Do not ask permission to commit on feature branches.
- Do ask before merging to main or pushing to remote.
- Section string constants live in app/ingestion/section_constants.py only, never as inline literals.
- All Groq API calls go through a shared async rate limiter, never called directly.
- EDGAR client must include User-Agent header on every request.

## Git Workflow
- Branch naming: feat/component-name, fix/issue-description
- One feature per branch
- PR before merge: `gh pr create`

## Session Protocol
- Start: /gsd-resume-work
- End: /gsd-pause-work then update ../vantage-notes/Vantage Build Log.md
- At 60% context: /compact
- At 90%: /gsd-pause-work then /clear

## Vertical Slice Rule
One complete slice per session:
implementation → tests → lint → typecheck → commit
Never scaffold multiple components at once.

## Autonomous Execution Protocol (VERY IMPORTANT)
- After each phase completes and PR is open, automatically 
  run /gsd-audit-milestone
- If audit passes, run /gsd-complete-milestone then move 
  to next phase and automatically run /gsd-discuss-phase, 
  /gsd-plan-phase, /gsd-execute-phase sequentially
- Only pause and wait for human when:
  1. A PR is ready to merge to main
  2. An audit fails and needs human judgment
  3. Something genuinely ambiguous not covered by 
     CONTEXT.md, SPEC.md, or CLAUDE.md
- Monitor context continuously. Run /gsd-pause-work then 
  /compact before hitting 60%. After compacting run 
  /gsd-resume-work and continue without waiting.
- Use installed skills as needed throughout execution. 
  Run /find-skills if a needed skill isn't loaded.
- After each phase completes, push the feature branch to 
  GitHub and open a PR against main using gh pr create.
  Include the phase name, what was built, and which GitHub 
  issues it closes in the PR description.
  Do not proceed to the next phase until the PR is open 
  on GitHub.