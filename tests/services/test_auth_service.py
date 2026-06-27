"""Unit tests for app.services.auth_service — RED phase.

All external calls (DB session, Redis) are mocked.
Tests cover:
- register_user: new email → returns (token, jti, exp); duplicate → HTTP 409
- login_user: correct creds → returns (token, jti, exp); bad creds → HTTP 401
- logout_user: calls redis.set with revoked:{jti} and positive TTL; Redis error → HTTP 503
- is_token_revoked: True when EXISTS > 0; False when EXISTS == 0
"""

import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_settings():
    """Return a minimal Settings-like object without requiring a real .env."""
    s = MagicMock()
    s.JWT_SECRET_KEY = "test-secret"
    s.JWT_ALGORITHM = "HS256"
    s.JWT_ACCESS_TOKEN_EXPIRE_SECONDS = 86400
    return s


def _make_user(email: str = "user@example.com", password: str = "hashed-pw"):
    """Return a mock User ORM instance."""
    u = MagicMock()
    u.id = uuid.uuid4()
    u.email = email
    u.password_hash = password
    return u


# ---------------------------------------------------------------------------
# register_user
# ---------------------------------------------------------------------------


class TestRegisterUser:
    @pytest.mark.asyncio
    async def test_new_email_returns_tuple(self):
        from app.services.auth_service import register_user

        settings = _make_settings()
        session = AsyncMock()
        # Simulate no existing user (scalars().first() returns None)
        session.execute.return_value = AsyncMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
        )

        with patch("app.services.auth_service.hash_password", return_value="hashed"):
            with patch(
                "app.services.auth_service.create_access_token",
                return_value=("tok", "jti-abc", int(time.time()) + 86400),
            ):
                result = await register_user("new@example.com", "pass", session, settings)

        assert isinstance(result, tuple)
        assert len(result) == 3
        token, jti, exp = result
        assert isinstance(token, str)
        assert isinstance(jti, str)
        assert isinstance(exp, int)

    @pytest.mark.asyncio
    async def test_duplicate_email_raises_409(self):
        from app.services.auth_service import register_user

        settings = _make_settings()
        session = AsyncMock()
        existing_user = _make_user()
        session.execute.return_value = AsyncMock(
            scalars=MagicMock(
                return_value=MagicMock(first=MagicMock(return_value=existing_user))
            )
        )

        with pytest.raises(HTTPException) as exc_info:
            await register_user("existing@example.com", "pass", session, settings)

        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_register_calls_hash_password(self):
        from app.services.auth_service import register_user

        settings = _make_settings()
        session = AsyncMock()
        session.execute.return_value = AsyncMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
        )

        with patch("app.services.auth_service.hash_password", return_value="bcrypt-hash") as mock_hp:
            with patch(
                "app.services.auth_service.create_access_token",
                return_value=("tok", "jti", int(time.time()) + 86400),
            ):
                await register_user("new@example.com", "plaintext", session, settings)

        mock_hp.assert_called_once_with("plaintext")


# ---------------------------------------------------------------------------
# login_user
# ---------------------------------------------------------------------------


class TestLoginUser:
    @pytest.mark.asyncio
    async def test_correct_credentials_returns_tuple(self):
        from app.services.auth_service import login_user

        settings = _make_settings()
        session = AsyncMock()
        user = _make_user(email="user@example.com", password="hashed-pw")
        session.execute.return_value = AsyncMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=user)))
        )

        with patch("app.services.auth_service.verify_password", return_value=True):
            with patch(
                "app.services.auth_service.create_access_token",
                return_value=("tok", "jti-xyz", int(time.time()) + 86400),
            ):
                result = await login_user("user@example.com", "correct-pass", session, settings)

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_wrong_password_raises_401(self):
        from app.services.auth_service import login_user

        settings = _make_settings()
        session = AsyncMock()
        user = _make_user()
        session.execute.return_value = AsyncMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=user)))
        )

        with patch("app.services.auth_service.verify_password", return_value=False):
            with pytest.raises(HTTPException) as exc_info:
                await login_user("user@example.com", "wrong-pass", session, settings)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_email_raises_401(self):
        from app.services.auth_service import login_user

        settings = _make_settings()
        session = AsyncMock()
        session.execute.return_value = AsyncMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
        )

        with pytest.raises(HTTPException) as exc_info:
            await login_user("ghost@example.com", "any-pass", session, settings)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_same_error_message_for_bad_creds(self):
        """User-not-found and wrong-password must return identical detail strings (T-01-04-04)."""
        from app.services.auth_service import login_user

        settings = _make_settings()

        # Case 1: user not found
        session1 = AsyncMock()
        session1.execute.return_value = AsyncMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
        )
        with pytest.raises(HTTPException) as exc1:
            await login_user("nobody@example.com", "any", session1, settings)

        # Case 2: wrong password
        session2 = AsyncMock()
        user = _make_user()
        session2.execute.return_value = AsyncMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=user)))
        )
        with patch("app.services.auth_service.verify_password", return_value=False):
            with pytest.raises(HTTPException) as exc2:
                await login_user("user@example.com", "wrong", session2, settings)

        assert exc1.value.detail == exc2.value.detail, (
            "Error messages must be identical to prevent username enumeration"
        )


# ---------------------------------------------------------------------------
# logout_user
# ---------------------------------------------------------------------------


class TestLogoutUser:
    @pytest.mark.asyncio
    async def test_calls_redis_set_with_revoked_key(self):
        from app.services.auth_service import logout_user

        redis_client = AsyncMock()
        jti = "test-jti"
        exp = int(time.time()) + 3600

        await logout_user(jti, exp, redis_client)

        redis_client.set.assert_called_once()
        call_kwargs = redis_client.set.call_args
        # Key must be "revoked:{jti}"
        assert call_kwargs[0][0] == f"revoked:{jti}" or call_kwargs[1].get("key") == f"revoked:{jti}"

    @pytest.mark.asyncio
    async def test_redis_set_ttl_is_positive(self):
        from app.services.auth_service import logout_user

        redis_client = AsyncMock()
        jti = "test-jti"
        exp = int(time.time()) + 3600

        await logout_user(jti, exp, redis_client)

        call_kwargs = redis_client.set.call_args
        # ex kwarg must be positive
        ex_value = call_kwargs[1].get("ex") or call_kwargs[0][2] if len(call_kwargs[0]) > 2 else None
        # Extract ex from keyword args
        ex_value = call_kwargs[1].get("ex")
        assert ex_value is not None
        assert ex_value >= 1, f"TTL must be >= 1, got {ex_value}"

    @pytest.mark.asyncio
    async def test_redis_error_raises_503(self):
        from app.services.auth_service import logout_user

        redis_client = AsyncMock()
        redis_client.set.side_effect = Exception("Redis connection refused")

        jti = "test-jti"
        exp = int(time.time()) + 3600

        with pytest.raises(HTTPException) as exc_info:
            await logout_user(jti, exp, redis_client)

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_expired_token_uses_minimum_ttl(self):
        """For already-expired tokens, TTL must be >= 1 (Redis requires positive ex)."""
        from app.services.auth_service import logout_user

        redis_client = AsyncMock()
        jti = "test-jti"
        # Token expired 10 seconds ago
        exp = int(time.time()) - 10

        await logout_user(jti, exp, redis_client)

        call_kwargs = redis_client.set.call_args
        ex_value = call_kwargs[1].get("ex")
        assert ex_value >= 1, f"Minimum TTL must be 1, got {ex_value}"


# ---------------------------------------------------------------------------
# is_token_revoked
# ---------------------------------------------------------------------------


class TestIsTokenRevoked:
    @pytest.mark.asyncio
    async def test_returns_true_when_key_exists(self):
        from app.services.auth_service import is_token_revoked

        redis_client = AsyncMock()
        redis_client.exists.return_value = 1

        result = await is_token_revoked("some-jti", redis_client)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_key_absent(self):
        from app.services.auth_service import is_token_revoked

        redis_client = AsyncMock()
        redis_client.exists.return_value = 0

        result = await is_token_revoked("some-jti", redis_client)
        assert result is False

    @pytest.mark.asyncio
    async def test_checks_correct_redis_key(self):
        from app.services.auth_service import is_token_revoked

        redis_client = AsyncMock()
        redis_client.exists.return_value = 0

        await is_token_revoked("my-jti", redis_client)

        redis_client.exists.assert_called_once_with("revoked:my-jti")
