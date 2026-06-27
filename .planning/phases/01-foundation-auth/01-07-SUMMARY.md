---
phase: 01-foundation-auth
plan: "07"
subsystem: test-infrastructure
status: complete
tags:
  - pytest
  - httpx
  - integration-tests
  - auth
  - conftest
dependency_graph:
  requires:
    - 01-05  # app/core/dependencies.py (get_settings, get_session, get_current_user, get_redis)
    - 01-04  # app/services/auth_service.py (register_user, login_user, logout_user, is_token_revoked)
    - 01-03  # app/db/models.py (User ORM), app/db/session.py (get_session)
    - 01-02  # app/core/config.py (Settings, get_settings)
    - 01-01  # app/models/auth.py (TokenResponse, UserOut)
  provides:
    - tests/conftest.py (test_settings, db_session, async_client fixtures)
    - tests/api/test_auth.py (12 async integration tests)
    - tests/api/__init__.py (package marker)
    - tests/db/__init__.py (package marker)
  affects:
    - 01-08  # can reuse async_client and test_settings fixtures from conftest.py
tech_stack:
  added:
    - httpx.AsyncClient + ASGITransport (ASGI in-process transport for integration tests)
    - sqlalchemy.ext.asyncio.create_async_engine (per-test engine for schema isolation)
    - unittest.mock.AsyncMock + patch (Redis mock for 503 test)
    - python-jose (jwt.decode for JWT expiry assertion)
  patterns:
    - per-function AsyncSession with create_all/drop_all schema isolation
    - FastAPI dependency_overrides for get_settings + get_session
    - Module-level patch (app.core.dependencies.aioredis) for Redis failure simulation
key_files:
  created:
    - tests/conftest.py
    - tests/api/__init__.py
    - tests/api/test_auth.py
    - tests/db/__init__.py
decisions:
  - "Used @pytest.fixture (not @pytest_asyncio.fixture) for async fixtures — pytest-asyncio 0.23.x asyncio_mode=auto supports @pytest.fixture for async fixtures; avoids import-time failure when pytest-asyncio is absent in dev environment"
  - "anyio_backend fixture preserved in conftest.py — existing unit tests (test_auth_service.py etc.) use @pytest.mark.anyio and require it"
  - "Patched app.core.dependencies.aioredis at module level for 503 test — get_redis and get_current_user both import aioredis from this module, so a single patch covers both the Depends(get_redis) call and the inline call in get_current_user"
  - "test_settings is session-scoped (Settings is immutable); db_session and async_client are function-scoped (each test gets isolated schema)"
metrics:
  duration: "7min"
  completed_date: "2026-06-27"
  tasks_completed: 2
  files_changed: 4
---

# Phase 01 Plan 07: Auth Integration Tests and Test Infrastructure Summary

**One-liner:** pytest conftest.py with async db_session/async_client fixtures + 12-function integration test suite covering all auth endpoints via httpx AsyncClient with ASGI transport.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Test infrastructure — conftest.py with async fixtures | e541022 | tests/conftest.py, tests/api/__init__.py, tests/db/__init__.py |
| 2 | Auth integration test suite | 5a74cf9 | tests/api/test_auth.py, tests/conftest.py (lint fix) |

## What Was Built

### Task 1 — tests/conftest.py

- `anyio_backend` fixture preserved — required by existing `@pytest.mark.anyio` unit tests
- `test_settings: Settings` (session-scoped) — Settings instance with TEST_DATABASE_URL (port 5433), TEST_REDIS_URL (DB 1), TEST_JWT_SECRET
- `db_session: AsyncSession` (function-scoped) — creates fresh async engine per test, runs `Base.metadata.create_all` before test and `drop_all` after; ensures schema isolation
- `async_client: AsyncClient` (function-scoped) — creates fresh FastAPI app via `create_app()`, overrides `get_settings` → test_settings and `get_session` → db_session, yields httpx AsyncClient with ASGITransport
- `import app.db.models` side-effect import — ensures all 9 ORM classes are registered in `Base.metadata` before `create_all` is called

### Task 2 — tests/api/test_auth.py (12 tests)

| Test | Endpoint | Assertion |
|------|----------|-----------|
| test_register_new_user | POST /register | 200 + access_token + token_type=bearer |
| test_register_duplicate_email | POST /register (x2) | 409 on second call |
| test_register_response_has_no_credential_fields | POST /register | keys exclude 'password' and 'password_hash' |
| test_login_correct | POST /login | 200 + access_token after register |
| test_login_wrong_password | POST /login | 401 |
| test_login_unknown_email | POST /login | 401 |
| test_me_authenticated | GET /me | 200 + email matches registered user |
| test_me_unauthenticated | GET /me | 401 or 403 (no header) |
| test_jwt_expiry_within_tolerance | decode token | exp - iat <= 86700 |
| test_logout_revokes_token | POST /logout → GET /me | 200 logout; 401 on subsequent /me |
| test_logout_requires_auth | POST /logout | 401 or 403 (no header) |
| test_redis_down_returns_503 | POST /logout | 503 when aioredis mock raises ConnectionError |

## Verification Results

```
$ python3 -m pytest tests/api/test_auth.py --collect-only -q
12 tests collected in 0.01s

$ python3 -c "from tests.conftest import test_settings, db_session, async_client"
# exits 0

$ python3 -c "import tests.conftest as c; assert hasattr(c, 'test_settings'); assert hasattr(c, 'db_session'); assert hasattr(c, 'async_client'); print('OK')"
OK
```

Full run (`pytest tests/api/test_auth.py -v`) requires test-postgres on port 5433 — passes when docker-compose.test.yml stack is running.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] pytest_asyncio not installed in local environment**
- **Found during:** Task 1 verification
- **Issue:** `import pytest_asyncio` would fail at conftest.py import time since pytest-asyncio is not installed globally (only in requirements/dev.txt for Docker)
- **Fix:** Used `@pytest.fixture` instead of `@pytest_asyncio.fixture`. In pytest-asyncio 0.23.x with asyncio_mode="auto", both decorators are equivalent for async fixtures
- **Files modified:** tests/conftest.py
- **Impact:** None — behavior is identical when pytest-asyncio is installed in Docker

**2. [Rule 1 - Bug] Unused `pytest` import in test_auth.py**
- **Found during:** Task 2 lint check
- **Issue:** `import pytest` was unused (no marks or fixtures directly imported)
- **Fix:** Removed unused import
- **Files modified:** tests/api/test_auth.py
- **Commit:** 5a74cf9

**3. [Rule 1 - Bug] Ruff I001 import order violation in conftest.py**
- **Found during:** Task 2 lint check
- **Issue:** `import app.db.models` was placed before third-party imports
- **Fix:** Moved to first-party import group (after third-party imports)
- **Files modified:** tests/conftest.py
- **Commit:** 5a74cf9

## Known Stubs

None — all test fixtures are fully wired. The test suite is a complete integration harness, not a scaffold.

## Threat Flags

No new trust-boundary surfaces. Test files introduce no production network endpoints. Threat T-01-07-02 (test DB isolation) is implemented via create_all/drop_all per fixture function.

## Self-Check: PASSED

- [x] tests/__init__.py exists (pre-existing)
- [x] tests/conftest.py exists and imports cleanly
- [x] tests/api/__init__.py exists
- [x] tests/api/test_auth.py exists with 12 test functions
- [x] tests/db/__init__.py exists
- [x] Task 1 commit: e541022
- [x] Task 2 commit: 5a74cf9
- [x] pytest --collect-only shows exactly 12 tests
- [x] ruff check passes
- [x] black --check passes
