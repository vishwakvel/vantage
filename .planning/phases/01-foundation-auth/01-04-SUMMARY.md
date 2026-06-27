---
phase: 01-foundation-auth
plan: "04"
subsystem: auth-service
status: complete
tags: [security, jwt, bcrypt, pydantic, auth-service]
dependency_graph:
  requires: [01-02]
  provides: [app/core/security.py, app/models/auth.py, app/services/auth_service.py]
  affects: [01-05, 01-08]
tech_stack:
  added:
    - bcrypt 5.0.0 (direct — passlib 1.7.4 incompatible with bcrypt 5.x)
    - python-jose 3.3.0 HS256 JWT encode/decode
    - redis.asyncio 5.0.4 async Redis client
  patterns:
    - bcrypt password hashing with direct library (not passlib CryptContext)
    - JWT 3-tuple return (token, jti, exp) for caller flexibility
    - Redis blocklist with TTL=max(1, exp-now) guard
    - Constant-time error message for auth failures (T-01-04-04)
key_files:
  created:
    - app/core/security.py
    - app/models/auth.py
    - app/services/auth_service.py
    - tests/core/test_security.py
    - tests/models/test_auth_models.py
    - tests/services/test_auth_service.py
    - tests/conftest.py
    - tests/__init__.py
    - tests/core/__init__.py
    - tests/models/__init__.py
    - tests/services/__init__.py
  modified: []
decisions:
  - "Use bcrypt library directly rather than passlib CryptContext — passlib 1.7.4 is incompatible with bcrypt 5.x (detect_wrap_bug internal test raises ValueError for passwords>72 bytes)"
  - "Use @pytest.mark.anyio instead of @pytest.mark.asyncio — pytest-asyncio not installed in current venv; anyio 4.9.0 plugin is available and supports asyncio backend"
  - "Restrict anyio backend to asyncio in conftest.py — trio not installed, would cause 14 test failures"
  - "logout_user uses max(1, exp - now) not max(0, ...) — Redis SET with ex=0 is an error in Redis 5.x"
metrics:
  duration: "11 minutes"
  completed: "2026-06-27"
  tasks_completed: 2
  files_created: 11
  tests_added: 47
---

# Phase 01 Plan 04: Auth Service Layer Summary

**One-liner:** bcrypt password hashing (direct library), HS256 JWT 3-tuple tokens, Pydantic auth models, and register/login/logout/revoke service with Redis blocklist and 503 guard.

## What Was Built

### Task 1: Security Module (app/core/security.py)

- `hash_password(password) -> str`: bcrypt via direct `bcrypt` library (not passlib). Returns `$2b$` prefix hash.
- `verify_password(plain, hashed) -> bool`: constant-time `bcrypt.checkpw`.
- `create_access_token(user_id, expires_seconds, secret_key, algorithm) -> tuple[str, str, int]`: encodes JWT with `{sub, jti, iat, exp}` payload; returns `(token, jti, exp)` 3-tuple. JTI is a UUIDv4 string.
- `decode_access_token(token, secret_key, algorithm) -> dict`: calls `jwt.decode(..., algorithms=[algorithm])` with a list — rejects `"none"` algorithm and any unsigned token.

18 unit tests covering all behaviors + TDD RED/GREEN cycle.

### Task 2: Pydantic Models (app/models/auth.py)

- `RegisterRequest`: email (EmailStr), password (str)
- `LoginRequest`: email (EmailStr), password (str)
- `TokenResponse`: access_token (str), token_type (str, default "bearer") — exactly two fields, no password or hash
- `UserOut`: id (UUID), email (str), `from_attributes=True` — no password or hash

15 unit tests covering all model constraints.

### Task 2: Auth Service (app/services/auth_service.py)

- `register_user(email, password, session, settings)`: normalises email to lowercase, checks for duplicate (409), hashes password with bcrypt, creates user, issues token.
- `login_user(email, password, session, settings)`: checks user existence and password in a single branch with identical error message for both failure modes (prevents username enumeration per T-01-04-04). Issues token on success.
- `logout_user(jti, exp, redis_client)`: computes `TTL = max(1, exp - now)`, calls `redis.set("revoked:{jti}", "", ex=TTL)`, raises HTTP 503 if Redis fails (never 200).
- `is_token_revoked(jti, redis_client)`: returns `redis.exists("revoked:{jti}") > 0`.

14 unit tests covering all service behaviors (mocked DB + Redis).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] passlib 1.7.4 incompatible with bcrypt 5.0.0**
- **Found during:** Task 1 GREEN phase — bcrypt tests failed immediately
- **Issue:** passlib's `detect_wrap_bug` initialisation routine hashes a 200+ character probe string; bcrypt 5.0.0 changed behaviour to raise `ValueError` for passwords > 72 bytes (older versions silently truncated). The error occurs during `CryptContext` first-use, not at import time.
- **Fix:** Replaced `CryptContext(schemes=["bcrypt"])` with direct `bcrypt` library calls (`bcrypt.hashpw`, `bcrypt.checkpw`). Identical security properties: bcrypt work factor, `$2b$` prefix, constant-time comparison.
- **Files modified:** `app/core/security.py`
- **Commit:** 8704315

**2. [Rule 3 - Blocker] pytest-asyncio not installed; anyio plugin available**
- **Found during:** Task 2 RED phase — async test decorators not recognized
- **Issue:** `pytest-asyncio==0.23.6` is listed in `requirements/dev.txt` but not installed in the active venv. `asyncio_mode = "auto"` in `pyproject.toml` shows it was intended. The anyio 4.9.0 plugin (already installed) supports `@pytest.mark.anyio`.
- **Fix:** Changed `@pytest.mark.asyncio` to `@pytest.mark.anyio` throughout `tests/services/test_auth_service.py`. Added `tests/conftest.py` with `anyio_backend` fixture restricted to `["asyncio"]` (trio not installed).
- **Files modified:** `tests/services/test_auth_service.py`, `tests/conftest.py`
- **Commit:** bcf132a

## Test Results

```
47 passed, 4 warnings in 2.18s
```

All 47 tests green across:
- `tests/core/test_security.py` — 18 tests
- `tests/models/test_auth_models.py` — 15 tests
- `tests/services/test_auth_service.py` — 14 tests (asyncio backend only)

TDD gate compliance: RED commits (`test(01-04)`) precede GREEN commits (`feat(01-04)`) in git log for both tasks.

## Security Invariants Verified

| STRIDE ID | Threat | Status |
|-----------|--------|--------|
| T-01-04-01 | Plaintext password stored | MITIGATED — bcrypt hash only |
| T-01-04-02 | JWT "none" algorithm accepted | MITIGATED — `algorithms=["HS256"]` list rejects none |
| T-01-04-03 | Silent logout on Redis failure | MITIGATED — HTTP 503 raised, never 200 |
| T-01-04-04 | Username enumeration via error diff | MITIGATED — identical "Invalid credentials" message |
| T-01-04-05 | Redis TTL shorter than token lifetime | MITIGATED — `max(1, exp - now)` guard |

## Known Stubs

None. All functions are fully implemented with real logic.

## Threat Flags

None. All surfaces introduced (password hashing, JWT, Redis blocklist) are within the plan's threat model.

## Self-Check: PASSED

All key files confirmed on disk:
- FOUND: app/core/security.py
- FOUND: app/models/auth.py
- FOUND: app/services/auth_service.py
- FOUND: tests/core/test_security.py
- FOUND: tests/models/test_auth_models.py
- FOUND: tests/services/test_auth_service.py

All commits confirmed in git log:
- FOUND: 3d2d391 (test: RED security module)
- FOUND: 8704315 (feat: GREEN security module)
- FOUND: d450081 (test: RED auth models + service)
- FOUND: bcf132a (feat: GREEN auth models + service)
