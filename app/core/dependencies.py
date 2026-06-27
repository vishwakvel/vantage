"""FastAPI dependency injection — shared across all protected routes.

Provides:
- ``bearer_scheme``: HTTPBearer security scheme (used by Depends on protected routes)
- ``get_redis``: yields an aioredis.Redis client from REDIS_URL
- ``get_current_user``: decodes JWT → checks Redis blocklist → fetches User → returns User

Security boundaries (STRIDE T-01-05-02, T-01-05-04):
- ``get_current_user`` validates JWT signature and rejects unsupported algorithms.
- Blocklist check precedes any user-data return so revoked tokens never grant access.
- All 401 paths use a single HTTP_401_UNAUTHORIZED status code.

Re-exports (convenience) — callers may import get_session / get_settings from here
instead of their original modules:
- ``get_session`` from app.db.session
- ``get_settings`` from app.core.config
"""

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_401_UNAUTHORIZED

import redis.asyncio as aioredis

from app.core.config import Settings, get_settings
from app.core.security import decode_access_token
from app.db.models import User
from app.db.session import get_session  # noqa: F401 — re-exported
from app.services.auth_service import is_token_revoked

# Module-level security scheme — injected into protected route signatures.
bearer_scheme = HTTPBearer()


async def get_redis(settings: Settings = Depends(get_settings)) -> aioredis.Redis:
    """Return an aioredis.Redis connection from ``settings.REDIS_URL``.

    Creates a connection on each call; acceptable for Phase 1 scope.
    Connection pooling is deferred to Phase 2+ if throughput warrants it.
    """
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User:
    """Validate a Bearer token and return the authenticated ``User``.

    Validation pipeline:
    1. Decode and verify JWT signature (rejects "none" alg, expired tokens).
    2. Extract ``sub`` (user_id) and ``jti`` claims; reject if either is missing.
    3. Check the Redis blocklist — raise 401 if the token has been revoked.
    4. Fetch the User from the database; raise 401 if not found.

    Args:
        credentials: Bearer token extracted from the ``Authorization`` header.
        session: Async SQLAlchemy session (injected via Depends).
        settings: Application settings (injected via Depends).

    Returns:
        The authenticated ``User`` ORM instance.

    Raises:
        HTTPException(401): On any validation failure (invalid token, missing
            claims, revoked JTI, or user not found in DB).
    """
    try:
        payload = decode_access_token(
            credentials.credentials,
            settings.JWT_SECRET_KEY,
            settings.JWT_ALGORITHM,
        )
    except JWTError:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id: str | None = payload.get("sub")
    jti: str | None = payload.get("jti")
    if not user_id or not jti:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid token claims",
        )

    # Blocklist check — must occur before any user-specific data is returned.
    redis = await get_redis(settings)
    if await is_token_revoked(jti, redis):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
        )

    result = await session.execute(select(User).where(User.id == user_id))
    user: User | None = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    return user


__all__ = [
    "bearer_scheme",
    "get_redis",
    "get_current_user",
    "get_session",
    "get_settings",
]
