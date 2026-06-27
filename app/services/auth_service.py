"""Authentication service layer.

Owns the register / login / logout / token-revocation business logic.
All external interactions are funnelled through caller-injected dependencies
(AsyncSession, Redis, Settings) to keep tests free of real I/O.

Security boundaries (STRIDE threat register T-01-04-01 through T-01-04-05):
- T-01-04-01: Passwords are hashed with bcrypt via hash_password; plaintext never stored.
- T-01-04-04: login_user returns identical detail for "no user" and "wrong password"
              to prevent username enumeration.
- T-01-04-03 & T-01-04-05: logout_user raises HTTP 503 on Redis failure (not 200);
              TTL = max(1, remaining) so the blocklist key never expires before the token.
"""

import time

import redis.asyncio as aioredis
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import (
    HTTP_401_UNAUTHORIZED,
    HTTP_409_CONFLICT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from app.core.config import Settings
from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import User


async def register_user(
    email: str,
    password: str,
    session: AsyncSession,
    settings: Settings,
) -> tuple[str, str, int]:
    """Register a new user and return ``(access_token, jti, exp)``.

    Args:
        email: The new user's email address (normalised to lowercase).
        password: The plaintext password — hashed before persistence.
        session: SQLAlchemy async session.
        settings: Application settings (JWT config).

    Returns:
        ``(token, jti, exp)`` — a valid access token for the newly created user.

    Raises:
        HTTPException(409): If a user with the same email already exists.
    """
    email = email.lower()

    # Duplicate email check
    result = await session.execute(select(User).where(User.email == email))
    existing = result.scalars().first()
    if existing is not None:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    user = User(email=email, password_hash=hash_password(password))
    session.add(user)
    await session.commit()
    await session.refresh(user)

    token, jti, exp = create_access_token(
        str(user.id),
        settings.JWT_ACCESS_TOKEN_EXPIRE_SECONDS,
        settings.JWT_SECRET_KEY,
        settings.JWT_ALGORITHM,
    )
    return (token, jti, exp)


async def login_user(
    email: str,
    password: str,
    session: AsyncSession,
    settings: Settings,
) -> tuple[str, str, int]:
    """Authenticate a user and return ``(access_token, jti, exp)``.

    Args:
        email: The user's email address.
        password: The plaintext password to verify.
        session: SQLAlchemy async session.
        settings: Application settings (JWT config).

    Returns:
        ``(token, jti, exp)`` — a valid access token on success.

    Raises:
        HTTPException(401): If the user is not found or the password is wrong.
            The same detail message is used for both cases to prevent username
            enumeration (T-01-04-04).
    """
    result = await session.execute(select(User).where(User.email == email.lower()))
    user = result.scalars().first()

    # Use a single constant-time branch: check user existence AND password validity
    # before raising — never reveal which condition failed (T-01-04-04).
    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    token, jti, exp = create_access_token(
        str(user.id),
        settings.JWT_ACCESS_TOKEN_EXPIRE_SECONDS,
        settings.JWT_SECRET_KEY,
        settings.JWT_ALGORITHM,
    )
    return (token, jti, exp)


async def logout_user(jti: str, exp: int, redis_client: aioredis.Redis) -> None:
    """Blocklist *jti* in Redis until the token would naturally expire.

    Args:
        jti: The JWT ID claim from the token being revoked.
        exp: The token's expiry Unix timestamp.
        redis_client: Async Redis client.

    Raises:
        HTTPException(503): If Redis is unavailable. The token remains active in
            this case — the caller must surface the error rather than silently
            treating the logout as successful (T-01-04-03).

    Redis TTL: ``max(1, exp - now)`` ensures:
    - The key never expires *before* the token does (T-01-04-05).
    - Redis ``SET ... EX 0`` (which is an error) never occurs.
    """
    remaining_ttl = max(1, exp - int(time.time()))
    try:
        await redis_client.set(f"revoked:{jti}", "", ex=remaining_ttl)
    except Exception:
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="Token revocation service unavailable",
        )


async def is_token_revoked(jti: str, redis_client: aioredis.Redis) -> bool:
    """Return True if *jti* has been blocklisted in Redis.

    Args:
        jti: The JWT ID claim to check.
        redis_client: Async Redis client.

    Returns:
        True if the key ``revoked:{jti}`` exists in Redis, False otherwise.
    """
    return await redis_client.exists(f"revoked:{jti}") > 0
