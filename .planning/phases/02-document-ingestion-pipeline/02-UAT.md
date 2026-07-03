---
status: complete
phase: 02-document-ingestion-pipeline
source: 02-01-SUMMARY.md, 02-02-SUMMARY.md, 02-03-SUMMARY.md, 02-04-SUMMARY.md, 02-05-SUMMARY.md, 02-06-SUMMARY.md
started: 2026-07-03T02:29:55Z
updated: 2026-07-03T02:56:00Z
---

## Current Test

[testing complete]

## Tests

### 0. Cold Start Smoke Test
expected: Kill any running server/service, clear ephemeral state, start the application from scratch via docker-compose, and confirm a primary health check returns live data.
result: pass
note: |
  Found 3 bugs on first attempt, all fixed and re-verified live:
  1. requirements/base.txt pinned pydantic==2.7.1, incompatible with
     langgraph==1.2.7 (needs >=2.7.4) -> bumped to 2.13.4.
  2. docker-compose.yml api service used .env's DATABASE_URL/REDIS_URL
     (both "localhost"), which doesn't resolve inside the compose network
     -> added environment: overrides using service hostnames
     (postgres/redis/chromadb).
  3. Dockerfile's HEALTHCHECK shells out to curl, but python:3.11-slim
     doesn't ship curl -> added apt-get install curl to Dockerfile.
  After fixes: `docker compose up -d --build` brings up all 4 services
  healthy; POST /api/v1/auth/register against the containerized api
  returned a real JWT, proving live Postgres connectivity over the
  docker network.

### 1. Auto-Ingest a Ticker (INGEST-01)
expected: POST /api/v1/ingest/ticker with {"ticker":"AAPL"} returns 200 with filings_ingested > 0 and stores indexed chunks — no manual upload required.
result: pass
note: |
  Found and fixed 3 real bugs in app/services/ingestion_service.py, all
  invisible to the mocked test suite:
  1. EFTS search used free-text q="{ticker}" which matches ANY filing
     mentioning the ticker string, not the company's own filings (top hit
     for "AAPL" was an unrelated filer) -> switched to entityName={ticker}.
  2. _source field extraction used form_type/period_of_report/cik/
     accession_no, none of which exist in the real EDGAR EFTS response
     (real fields: form/root_forms, period_ending, ciks, adsh) -> fixed
     field mapping; updated _SEARCH_RESPONSE_DATA test fixture to match.
  3. _find_primary_doc assumed a "{accession}-index.json" file and a
     directory-order heuristic that picks the WRONG file (real listing has
     exhibits before the primary doc, and the self-index page's real name
     "{accession}-index-headers.html" slips past the old suffix check) ->
     rewrote to fetch FilingSummary.xml and read the authoritative
     <Report instance="..."> attribute instead.
  Also found and fixed 2 infra bugs surfaced only by real service calls:
  4. chromadb client 0.5.23 probes /api/v2/auth/identity on connect, which
     the pinned chromadb/chroma:0.5.0 server image doesn't have -> bumped
     docker-compose.yml's chromadb image to 0.5.23 to match the client.
  5. Document.ticker has an FK to companies.ticker, but no code path ever
     created a Company row for a new ticker (ticker_resolver only matches
     against an in-memory seed list, never persists it) -> added
     _ensure_company_exists() upsert, called from both ingest_ticker and
     ingest_pdf before any Document insert.
  Verified clean end-to-end for ticker MSFT (never touched before):
  filings_ingested=4, filings_cached=0, source_warnings=[].
  Full suite re-run after all fixes: 221 passed, 0 failed.

### 2. Second Trigger Reuses Cached Filings (INGEST-02)
expected: A second POST /api/v1/ingest/ticker for AAPL returns filings_cached > 0 and filings_ingested = 0 — no EDGAR re-fetch.
result: pass
note: |
  Second MSFT trigger: filings_ingested=0, filings_cached=4,
  source_warnings=[]. Request completed in 0.038s (vs 23.4s on the first
  call) — confirms zero EDGAR network calls on the cached path.

### 3. Private PDF Upload Is User-Isolated (INGEST-03)
expected: An authenticated user uploads a private PDF via POST /api/v1/ingest/pdf; the response is 200 with filings_ingested = 1, stored under that user's id.
result: pass
note: |
  userA uploaded a private PDF for ticker ACME: filings_ingested=1.
  Live hybrid_retrieve() query for the same text against both users:
  userA (uploader) got 1 result with the real content; userB got 0
  results. Isolation proven against real ChromaDB, not a mock.

### 4. Non-Fatal Source Warning on Ingestion Failure (INGEST-04)
expected: Ingesting an invalid/unreachable ticker returns a 4xx/200-with-warning response, not a 500 error.
result: pass
note: |
  POST /ingest/ticker {"ticker":"ZZZZZ"} (EDGAR itself 500s on this
  nonsense entityName search) -> our API still returned HTTP 200 with
  filings_ingested=0 and a source_warnings entry describing the EDGAR
  failure. No 5xx propagated to the client.

### 5. Cross-Source Dedup (INGEST-05)
expected: Uploading the same filing as a private PDF that's already EDGAR-indexed produces filings_cached=1, no duplicate chunk set.
result: pass
note: |
  Uploaded a PDF for MSFT 10-K 2025-06-30, already EDGAR-indexed by Test 1.
  Response: filings_ingested=0, filings_cached=1. Confirmed via direct
  Postgres query: exactly one `documents` row for that canonical_id
  (source_type=EDGAR) — no duplicate row from the PDF path.

## Summary

total: 6
passed: 6
issues: 0
pending: 0
skipped: 0

## Gaps

None. All 5 bugs found during live verification (3 in ingestion_service.py,
2 in docker/dependency config) were fixed and re-verified in this same
session — see notes on Tests 0 and 1 above.
