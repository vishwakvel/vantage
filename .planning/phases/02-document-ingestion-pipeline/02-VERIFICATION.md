---
phase: 02-document-ingestion-pipeline
verified: 2026-07-03T03:00:26Z
status: passed
score: 5/5 success criteria verified
behavior_unverified: 0
overrides_applied: 0
---

# Phase 2: Document Ingestion Pipeline Verification Report

**Phase Goal:** EDGAR filings are automatically fetched, chunked, and indexed into the hybrid RAG pipeline when a ticker is researched; previously ingested filings are reused; private user PDFs are isolated; deduplication prevents double-ingestion
**Mode:** mvp
**User Story (retroactively assigned via `/gsd mvp-phase 02` for this verification):** "As a retail investor, I want to have SEC filings auto-fetched, chunked, and indexed for hybrid RAG retrieval, with reuse, private-doc isolation, and dedup, so that my research memo is grounded in accurate, up-to-date source documents without me lifting a finger."
**Verified:** 2026-07-03T03:00:26Z
**Status:** passed
**Re-verification:** No — this phase shipped 2026-06-28 without ever producing a VERIFICATION.md (the verify step was skipped during original execution). This is the first verification, performed retroactively as part of closing Milestone v1.0.

## Verification Method — Live, Not Mocked

Every other phase's verification in this project was performed by inspecting code and running the (fully mocked) unit test suite. This phase's verification instead ran the actual `docker compose` stack (real PostgreSQL, real Redis, real ChromaDB, real containerized API) and made real HTTP calls against the live SEC EDGAR API — because the entire Phase 2 test suite mocks EDGAR, ChromaDB, and the DB session, so a purely code+test-based verification could not have caught any of the bugs found below. All five ROADMAP Success Criteria and the underlying EDGAR integration were broken in ways invisible to the mocked test suite; none of the 6 SUMMARY.md files' claims held up against a real environment on first attempt.

**Bugs found and fixed during this verification** (see `.planning/phases/02-document-ingestion-pipeline/02-UAT.md` for full detail and `fix(02): repair EDGAR ingestion pipeline and docker-compose cold start` (PR #25, merged) for the code):

1. EFTS ticker search used free-text `q="{ticker}"` (matches any filing mentioning the string, not the company's own filings) → switched to `entityName={ticker}`.
2. `_source` field extraction read `form_type`/`period_of_report`/`cik`/`accession_no`, none of which exist in the real EDGAR EFTS response (`form`/`root_forms`, `period_ending`, `ciks`, `adsh`) → fixed field mapping.
3. `_find_primary_doc` assumed a non-existent `"{accession}-index.json"` filename and a directory-order heuristic that selects the wrong file → rewritten to read the authoritative `<Report instance="...">` from `FilingSummary.xml`.
4. `Document.ticker`'s FK to `companies.ticker` had no code path that ever created a `Company` row for a new ticker → added `_ensure_company_exists()` upsert.
5. Infra: `pydantic` pin incompatible with `langgraph`, `docker-compose.yml` api service couldn't resolve peer services by hostname, Dockerfile healthcheck missing `curl`, ChromaDB client/server version mismatch — all fixed (see commit for full list).

After all fixes, the live walkthrough below was re-run clean.

## User Flow Coverage

| Step | Expected | Evidence | Status |
|------|----------|----------|--------|
| Investor's research triggers auto-fetch | `POST /api/v1/ingest/ticker {"ticker":"MSFT"}` against the live containerized API, no prior state | Live curl: `filings_ingested=4, filings_cached=0, source_warnings=[]` (23.4s, real EDGAR fetch) | ✓ |
| Re-research reuses indexed filings | Second identical call | Live curl: `filings_ingested=0, filings_cached=4` in 0.038s — zero EDGAR calls | ✓ |
| Private PDF stays private | userA uploads a PDF; userB queries the same terms | Live `hybrid_retrieve()`: userA gets 1 result (real content), userB gets 0 | ✓ |
| A failing source doesn't break the memo | Ingest a ticker EDGAR itself 500s on | HTTP 200 from our API, `source_warnings` populated, no 5xx | ✓ |
| Same filing via two sources dedups | PDF upload of an already-EDGAR-indexed filing | `filings_cached=1`; direct Postgres query confirms exactly one `documents` row | ✓ |

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria — the contract)

| # | Truth (SC) | Status | Evidence |
|---|---------|--------|----------|
| 1 | Triggering research for AAPL automatically fetches recent 10-K/10-Q filings from EDGAR and stores indexed chunks in ChromaDB — no manual upload required | ✓ VERIFIED | Live: `POST /api/v1/ingest/ticker {"ticker":"MSFT"}` (fresh ticker) → `filings_ingested=4, source_warnings=[]`. Confirmed real EDGAR fetch (23.4s round-trip, real accession numbers/dates in response) and real ChromaDB storage (subsequent `hybrid_retrieve` calls return the ingested content). |
| 2 | A second research trigger for AAPL reuses already-indexed chunks and does not call EDGAR fetch service | ✓ VERIFIED | Live: second identical call → `filings_ingested=0, filings_cached=4`, 0.038s (vs 23.4s first call) — the ~600x speedup is direct evidence of zero EDGAR network calls on the cached path. |
| 3 | A user's uploaded private PDF returns chunks under that user's metadata filter and zero under any other user's | ✓ VERIFIED | Live: userA uploads PDF for ticker ACME (`filings_ingested=1`). Direct `hybrid_retrieve()` call (real ChromaDB, not mocked): userA query returns 1 result with the real uploaded content; identical query as userB returns 0 results. |
| 4 | When ingestion from a specific source fails, API returns a non-fatal source warning before the memo is attempted — no 500 error | ✓ VERIFIED | Live: `POST /api/v1/ingest/ticker {"ticker":"ZZZZZ"}` — EDGAR's own EFTS search 500s on this nonsense entity name. Our API still returned HTTP 200 with `source_warnings: ["EDGAR search failed for ZZZZZ: ..."]`. No 5xx propagated. |
| 5 | Uploading the same 10-K as a user PDF alongside an auto-ingested EDGAR copy produces exactly one DocumentChunk set, keyed by canonical_id — no duplicate rows | ✓ VERIFIED | Live: uploaded a PDF for MSFT 10-K 2025-06-30 (already EDGAR-indexed by SC1's test). Response: `filings_cached=1`. Direct Postgres query (`SELECT ... FROM documents WHERE ticker='MSFT' AND title LIKE '%10-K%'`) confirms exactly 1 row, `source_type=EDGAR`, no duplicate row from the PDF path. |

**Score:** 5/5 ROADMAP Success Criteria verified live against real services (not mocks).

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `app/services/vector_store.py` | ChromaDB singleton, user-isolation, dedup | ✓ VERIFIED | Present; live-tested user isolation via real `hybrid_retrieve` calls |
| `app/services/edgar_client.py` | `get()` + `get_archive()`, EDGAR User-Agent | ✓ VERIFIED | Present; live EDGAR calls all carried the required User-Agent (SEC never rejected a request) |
| `app/ingestion/chunker.py` | `section_aware_chunk`, `word_split`, `ITEM_TO_SECTION` | ✓ VERIFIED | Present; real MSFT/AAPL 10-K/10-Q HTML chunked without error across all live ingests |
| `app/ingestion/retriever.py` | `hybrid_retrieve`, RRF, BM25, reranker | ✓ VERIFIED | Present; live-invoked directly for the isolation proof, returned correctly-ranked real content |
| `app/services/ingestion_service.py` | `ingest_ticker`, `ingest_pdf`, dedup, non-fatal warnings | ✓ VERIFIED — required 3 bug fixes | EDGAR search/field-mapping/primary-doc bugs fixed this session; all 5 success criteria now hold live |
| `app/api/v1/ingest.py` | `POST /ingest/ticker`, `POST /ingest/pdf` | ✓ VERIFIED | Both endpoints live-tested via curl against the containerized API |
| `docker-compose.yml` (Phase 1 asset, exercised here) | 4-service stack boots clean | ✓ VERIFIED — required 4 bug fixes | pydantic pin, api networking, Dockerfile curl, chromadb image version all fixed this session |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full docker-compose stack builds and boots healthy | `docker compose up -d --build` | All 4 containers report `healthy` | ✓ PASS |
| Fresh-ticker ingest end-to-end | `curl -X POST .../ingest/ticker -d '{"ticker":"MSFT"}'` | `filings_ingested=4, source_warnings=[]` | ✓ PASS |
| Cache reuse | Same call repeated | `filings_ingested=0, filings_cached=4`, 0.038s | ✓ PASS |
| Cross-user isolation | `hybrid_retrieve()` for userA vs userB | 1 result vs 0 results | ✓ PASS |
| Non-fatal failure handling | `curl -X POST .../ingest/ticker -d '{"ticker":"ZZZZZ"}'` | HTTP 200, warning present | ✓ PASS |
| Cross-source dedup | PDF upload of an EDGAR-indexed filing | `filings_cached=1`, 1 DB row confirmed via psql | ✓ PASS |
| Full test suite after all fixes | `pytest tests/ -q` | 221 passed, 0 failed | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| INGEST-01 | 02-01 through 02-06 | SEC filings auto-ingested when a ticker is researched | ✓ SATISFIED | Live SC1 above |
| INGEST-02 | 02-03 | Previously ingested docs reused without re-fetching | ✓ SATISFIED | Live SC2 above |
| INGEST-03 | 02-01, 02-04, 02-05 | Private uploads invisible to other users | ✓ SATISFIED | Live SC3 above |
| INGEST-04 | 02-03, 02-06 | Source failures surfaced non-fatally | ✓ SATISFIED | Live SC4 above |
| INGEST-05 | 02-03, 02-05 | Cross-source dedup via canonical_id | ✓ SATISFIED | Live SC5 above |

All five requirements are marked `[x]` Complete in REQUIREMENTS.md's Phase 2 traceability table, consistent with this verification.

### Anti-Patterns Found

None remaining. The bugs found during this verification were factual/integration errors (wrong field names, wrong API parameters, wrong file paths, missing FK-satisfying rows) — not stubs, TODOs, or placeholder code. All were root-caused against the real EDGAR API and real service responses, fixed, and re-verified live.

### Human Verification Required

None. This is an API-only phase (curl/Postman-verifiable per PROJECT.md/ROADMAP.md's explicit design). All observable truths were verified directly against live services and real HTTP responses — a stronger verification standard than the code+mocked-test method used for the project's other phases.

### Gaps Summary

No gaps remaining. All 5 ROADMAP Success Criteria are verified against real EDGAR, real ChromaDB, and real PostgreSQL — not code inspection or mocked assertions. The phase's original SUMMARY.md files significantly overstated confidence (all 6 claimed "Self-Check: PASSED" while the core EDGAR integration was non-functional against the live API); this verification's evidence supersedes those claims. Five bugs were found and fixed in this session (3 in `ingestion_service.py`, 2 in Docker/dependency config); see PR #25 (merged to `main`) for the full diff and `02-UAT.md` for the complete bug-by-bug narrative.

---

_Verified: 2026-07-03T03:00:26Z_
_Verifier: Claude (live end-to-end walkthrough, not gsd-verifier code-inspection agent)_
