"""Authentication API endpoints (APIRouter: prefix=/auth).

Endpoints:
- POST /auth/register  → 200 TokenResponse (or 409 on duplicate email)
- POST /auth/login     → 200 TokenResponse (or 401 on bad credentials)
- POST /auth/logout    → 200 {detail} (or 401/503)
- GET  /auth/me        → 200 UserOut (or 401 on invalid/revoked token)

Security boundaries (STRIDE T-01-05-01, T-01-05-02, T-01-05-03):
- register/login responses are serialised via ``response_model=TokenResponse``
  which excludes all credential fields (T-01-05-01).
- /me and /logout both depend on ``get_current_user`` which validates the JWT
  AND checks the Redis blocklist before any user data is returned (T-01-05-02).
- logout calls get_current_user first; only after validation does it blocklist
  the JTI — ensuring a 503 from logout_user surfaces rather than silently
  leaving the token active (T-01-05-03).
"""

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.dependencies import (
    bearer_scheme,
    get_current_user,
    get_redis,
    get_session,
    get_settings,
)
from app.core.security import decode_access_token
from app.db.models import User
from app.models.auth import LoginRequest, RegisterRequest, TokenResponse, UserOut
from app.services.auth_service import login_user, logout_user, register_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse)
async def register(
    body: RegisterRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """Register a new user and return an access token.

    Args:
        body: Email and plaintext password.
        session: Injected async DB session.
        settings: Injected application settings.

    Returns:
        ``TokenResponse(access_token=..., token_type='bearer')``

    Raises:
        HTTPException(409): If the email is already registered.
    """
    token, _jti, _exp = await register_user(
        body.email, body.password, session, settings
    )
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """Authenticate a user and return an access token.

    Args:
        body: Email and plaintext password.
        session: Injected async DB session.
        settings: Injected application settings.

    Returns:
        ``TokenResponse(access_token=..., token_type='bearer')``

    Raises:
        HTTPException(401): If credentials are invalid (same message for
            "user not found" and "wrong password" to prevent enumeration).
    """
    token, _jti, _exp = await login_user(
        body.email, body.password, session, settings
    )
    return TokenResponse(access_token=token)


@router.post("/logout", status_code=200)
async def logout(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),  # validates before blocklisting
) -> dict:
    """Revoke the caller's token by adding its JTI to the Redis blocklist.

    ``current_user`` is resolved first — the token is validated AND the blocklist
    is checked before we attempt revocation.  Only a currently valid token can
    trigger a revocation (T-01-05-03).

    The token is decoded a second time solely to extract ``jti`` and ``exp``
    for the TTL calculation; the signature was already verified above so the
    second decode is safe.

    Args:
        credentials: Bearer token from Authorization header.
        settings: Injected application settings (JWT config).
        redis: Injected async Redis client.
        current_user: Resolved only for its side-effect of validating the token.

    Returns:
        ``{"detail": "Logged out"}`` on success.

    Raises:
        HTTPException(401): If the token is invalid or already revoked.
        HTTPException(503): If Redis is unreachable during revocation.
    """
    payload = decode_access_token(
        credentials.credentials,
        settings.JWT_SECRET_KEY,
        settings.JWT_ALGORITHM,
    )
    await logout_user(payload["jti"], payload["exp"], redis)
    return {"detail": "Logged out"}


@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)) -> User:
    """Return the authenticated user's public profile.

    Args:
        current_user: Validated and blocklist-checked User from Depends.

    Returns:
        ``UserOut(id=..., email=...)`` — no credential fields.

    Raises:
        HTTPException(401): If the Bearer token is missing, invalid, expired, or
            has been revoked.
    """
    return current_user
