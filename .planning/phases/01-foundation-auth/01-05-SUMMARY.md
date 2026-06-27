---
phase: 01-foundation-auth
plan: "05"
subsystem: auth-api
status: complete
tags:
  - fastapi
  - dependency-injection
  - auth
  - jwt
  - redis
dependency_graph:
  requires:
    - 01-04  # auth_service.py (register_user, login_user, logout_user, is_token_revoked)
    - 01-03  # app/db/models.py (User ORM), app/db/session.py (get_session)
    - 01-02  # app/core/config.py (Settings, get_settings), app/core/security.py
    - 01-01  # app/models/auth.py (TokenResponse, UserOut, RegisterRequest, LoginRequest)
  provides:
    - app/core/dependencies.py (bearer_scheme, get_redis, get_current_user)
    - app/api/v1/auth.py (router with register/login/logout/me)
    - app/api/v1/__init__.py (v1 router)
    - app/main.py (updated — v1_router mounted at /api/v1)
  affects:
    - 01-07  # integration tests will drive these endpoints end-to-end
tech_stack:
  added:
    - fastapi.security.HTTPBearer (bearer_scheme)
    - redis.asyncio (aioredis.from_url for Redis blocklist)
  patterns:
    - FastAPI Depends() injection chain for auth (bearer_scheme → get_current_user)
    - response_model=TokenResponse to strip credential fields from response
    - Blocklist-before-return ordering in get_current_user and logout
key_files:
  created:
    - app/core/dependencies.py
    - app/api/v1/auth.py
    - app/api/v1/__init__.py (rewritten from empty stub)
  modified:
    - app/main.py
decisions:
  - "get_current_user calls get_redis inline (not via Depends) to avoid nested Depends limitations when called from endpoint handlers that also need redis independently"
  - "logout endpoint depends on get_current_user for validation BEFORE calling logout_user — ensures 503 surfaces rather than silently leaving an invalid token active (T-01-05-03)"
  - "decode_access_token called twice in logout: first by get_current_user (validation), second by the endpoint (to extract jti/exp for TTL) — safe because signature already verified"
metrics:
  duration: "5min"
  completed_date: "2026-06-27"
  tasks_completed: 2
  files_changed: 4
---

# Phase 01 Plan 05: Auth API Endpoints and Dependency Injection Summary

**One-liner:** FastAPI Bearer auth dependency chain (get_redis → is_token_revoked → get_current_user) with register/login/logout/me endpoints mounted at /api/v1/auth.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | FastAPI dependency injection (get_redis, get_current_user) | 6320211 | app/core/dependencies.py |
| 2 | Auth router endpoints + wire into main.py | 5bd2989 | app/api/v1/auth.py, app/api/v1/__init__.py, app/main.py |

## What Was Built

### Task 1 — app/core/dependencies.py

- `bearer_scheme = HTTPBearer()` — module-level security scheme for injection into protected routes
- `get_redis(settings) -> aioredis.Redis` — creates connection from `settings.REDIS_URL` (Phase 1 per-call connection; pooling deferred to Phase 2)
- `get_current_user(credentials, session, settings) -> User` — full validation pipeline:
  1. Decode JWT (rejects `alg=none`, expired, tampered — T-01-05-04)
  2. Extract and validate `sub` (user_id) and `jti` claims
  3. Check Redis blocklist via `is_token_revoked(jti, redis)` — raises 401 if revoked (T-01-05-02)
  4. DB lookup via `select(User).where(User.id == user_id)` — ORM parameterized query (T-01-05-05)
  5. Return User or raise 401
- Re-exports `get_session` and `get_settings` so callers use a single import location

### Task 2 — app/api/v1/auth.py + __init__.py + main.py

- `POST /api/v1/auth/register` — delegates to `register_user()`, responds with `response_model=TokenResponse` (no password/hash fields — T-01-05-01)
- `POST /api/v1/auth/login` — delegates to `login_user()`, same response model
- `POST /api/v1/auth/logout` — resolves `get_current_user` first (validates + blocklist check), then calls `logout_user(jti, exp, redis)` — 503 surfaces if Redis unreachable (T-01-05-03)
- `GET /api/v1/auth/me` — returns `UserOut(id, email)` via `response_model=UserOut`; requires valid non-revoked token
- `app/api/v1/__init__.py` — replaced empty stub with `router = APIRouter()` including auth_router
- `app/main.py` — replaced `try/except ImportError` stub with direct `from app.api.v1 import router as v1_router` + `include_router`

## Verification Results

```
All auth routes present: ['/health', '/api/v1/auth/register', '/api/v1/auth/login', '/api/v1/auth/logout', '/api/v1/auth/me']
dependencies import OK
router prefix: /auth
```

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None — all endpoints are fully wired to service functions. No placeholder responses.

## Threat Flags

No new trust-boundary surfaces beyond those in the plan's STRIDE register. All five threats (T-01-05-01 through T-01-05-05) are implemented as specified.

## Self-Check: PASSED

- [x] app/core/dependencies.py exists and imports cleanly
- [x] app/api/v1/auth.py exists with router prefix="/auth"
- [x] app/api/v1/__init__.py exports router
- [x] app/main.py mounts v1_router at /api/v1
- [x] All 4 auth routes + /health present in create_app() routes
- [x] Task 1 commit: 6320211
- [x] Task 2 commit: 5bd2989
