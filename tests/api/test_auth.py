"""Auth API integration tests — full coverage of register/login/logout/me endpoints.

All tests use the async_client fixture from conftest.py which:
- Points at test-postgres on port 5433 (not real DB)
- Overrides get_settings and get_session via dependency_overrides
- Creates and drops the schema around each test function

Prohibitions validated:
1. Response JSON never includes 'password' or 'password_hash' fields
2. Duplicate email registration returns 409
3. Logout with Redis down returns 503 (token revocation service unavailable)
4. Revoked tokens are rejected with 401 on subsequent requests
"""

from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from jose import jwt as jose_jwt

# ---------------------------------------------------------------------------
# URL constants
# ---------------------------------------------------------------------------

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/auth/me"

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def register_and_get_token(
    client: AsyncClient,
    email: str = "user@test.com",
    password: str = "Pass1234!",
) -> str:
    """Register a new user and return the access token.

    Args:
        client: Authenticated async HTTP client.
        email: Email address for the new user.
        password: Plaintext password for the new user.

    Returns:
        JWT access token string.
    """
    resp = await client.post(REGISTER_URL, json={"email": email, "password": password})
    assert resp.status_code == 200, f"Registration failed: {resp.json()}"
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Register tests
# ---------------------------------------------------------------------------


async def test_register_new_user(async_client: AsyncClient):
    """POST /register with a fresh email returns 200 with access_token."""
    resp = await async_client.post(
        REGISTER_URL, json={"email": "new@test.com", "password": "Secret123"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


async def test_register_duplicate_email(async_client: AsyncClient):
    """Registering the same email twice returns 409 on the second call."""
    payload = {"email": "dup@test.com", "password": "Secret123"}
    first = await async_client.post(REGISTER_URL, json=payload)
    assert first.status_code == 200
    resp = await async_client.post(REGISTER_URL, json=payload)
    assert resp.status_code == 409


async def test_register_response_has_no_credential_fields(async_client: AsyncClient):
    """Registration response JSON must not include 'password' or 'password_hash'.

    Validates SPEC prohibition: credentials are never serialised in any API response.
    """
    resp = await async_client.post(
        REGISTER_URL, json={"email": "safe@test.com", "password": "Secret123"}
    )
    assert resp.status_code == 200
    keys = set(resp.json().keys())
    assert "password" not in keys, "Response must not expose plaintext password"
    assert "password_hash" not in keys, "Response must not expose password hash"


# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------


async def test_login_correct(async_client: AsyncClient):
    """Register then login with same credentials returns 200 + access_token."""
    await async_client.post(REGISTER_URL, json={"email": "login@test.com", "password": "Pass1234"})
    resp = await async_client.post(
        LOGIN_URL, json={"email": "login@test.com", "password": "Pass1234"}
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


async def test_login_wrong_password(async_client: AsyncClient):
    """Login with wrong password returns 401."""
    await async_client.post(REGISTER_URL, json={"email": "wp@test.com", "password": "Correct1"})
    resp = await async_client.post(LOGIN_URL, json={"email": "wp@test.com", "password": "Wrong"})
    assert resp.status_code == 401


async def test_login_unknown_email(async_client: AsyncClient):
    """Login with an email not in the DB returns 401."""
    resp = await async_client.post(LOGIN_URL, json={"email": "nobody@test.com", "password": "Pass"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Me endpoint tests
# ---------------------------------------------------------------------------


async def test_me_authenticated(async_client: AsyncClient, test_settings):
    """GET /me with a valid token returns 200 and the registered user's email."""
    token = await register_and_get_token(async_client, "me@test.com")
    resp = await async_client.get(ME_URL, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "me@test.com"


async def test_me_unauthenticated(async_client: AsyncClient):
    """GET /me without Authorization header returns 401 or 403."""
    resp = await async_client.get(ME_URL)
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# JWT expiry test
# ---------------------------------------------------------------------------


async def test_jwt_expiry_within_tolerance(async_client: AsyncClient, test_settings):
    """Decoded JWT exp - iat must not exceed 86700 seconds (24h + 5min tolerance)."""
    token = await register_and_get_token(async_client)
    payload = jose_jwt.decode(
        token,
        test_settings.JWT_SECRET_KEY,
        algorithms=[test_settings.JWT_ALGORITHM],
    )
    delta = payload["exp"] - payload["iat"]
    assert delta <= 86700, f"JWT lifetime {delta}s exceeds tolerance of 86700s"


# ---------------------------------------------------------------------------
# Logout tests
# ---------------------------------------------------------------------------


async def test_logout_revokes_token(async_client: AsyncClient):
    """After logout the same token must be rejected with 401.

    Validates SPEC prohibition: revoked tokens never grant access to /me.
    """
    token = await register_and_get_token(async_client, "logout@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await async_client.post(LOGOUT_URL, headers=headers)
    assert resp.status_code == 200

    # Same token must now be rejected by the blocklist check in get_current_user
    resp2 = await async_client.get(ME_URL, headers=headers)
    assert resp2.status_code == 401


async def test_logout_requires_auth(async_client: AsyncClient):
    """POST /logout without Authorization header returns 401 or 403."""
    resp = await async_client.post(LOGOUT_URL)
    assert resp.status_code in (401, 403)


async def test_redis_down_returns_503(async_client: AsyncClient):
    """POST /logout returns 503 when the Redis blocklist is unreachable.

    Validates SPEC prohibition: never return 200 on logout if the token cannot
    be revoked (T-01-04-03 / T-01-05-03).

    Patch strategy:
    - app.core.dependencies.aioredis.from_url returns a mock Redis client.
    - mock client.exists returns 0 (token not yet revoked; get_current_user passes).
    - mock client.set raises ConnectionError (logout_user raises 503).
    """
    token = await register_and_get_token(async_client, "redisdown@test.com")
    headers = {"Authorization": f"Bearer {token}"}

    mock_client = AsyncMock()
    mock_client.set = AsyncMock(side_effect=ConnectionError("Redis down"))
    mock_client.exists = AsyncMock(return_value=0)  # token not in blocklist

    with patch("app.core.dependencies.aioredis") as mock_redis_module:
        mock_redis_module.from_url.return_value = mock_client
        resp = await async_client.post(LOGOUT_URL, headers=headers)

    assert (
        resp.status_code == 503
    ), f"Expected 503 on Redis failure, got {resp.status_code}: {resp.json()}"
