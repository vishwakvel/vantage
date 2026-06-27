"""Password hashing and JWT encode/decode utilities.

Security boundaries (per SPEC prohibitions and STRIDE T-01-04-01, T-01-04-02):
- Passwords are always hashed with bcrypt — plaintext is never stored.
- JWT decode enforces a concrete algorithm list; the "none" algorithm is rejected
  because it is not in the list passed to jwt.decode.
- create_access_token returns a (token, jti, exp) 3-tuple; exp - iat is bounded
  by the caller-supplied expires_seconds (SPEC: max 86400 → 86700 with tolerance).

Implementation note: bcrypt library is used directly (not via passlib) because
passlib 1.7.4's internal wrap-bug detection routine is incompatible with bcrypt 5.x
which raises ValueError for passwords > 72 bytes during the init probe.
The direct bcrypt API provides identical security properties.
"""

import uuid
from datetime import UTC, datetime

import bcrypt as _bcrypt
from jose import JWTError, jwt  # noqa: F401 — re-exported so callers can catch JWTError


def hash_password(password: str) -> str:
    """Return a bcrypt hash of *password*.

    Never call this with an already-hashed password — bcrypt will hash the hash.
    The returned string starts with ``$2b$`` (bcrypt version 2b prefix).
    Uses bcrypt with default work factor (12 rounds).
    """
    hashed: bytes = _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return True if *plain_password* matches *hashed_password*.

    bcrypt.checkpw uses a constant-time comparison internally to resist timing attacks.
    """
    return _bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


def create_access_token(
    user_id: str,
    expires_seconds: int,
    secret_key: str,
    algorithm: str,
) -> tuple[str, str, int]:
    """Encode a JWT access token for *user_id* and return ``(token, jti, exp)``.

    Args:
        user_id: The subject claim (``sub``).  Typically a UUID string.
        expires_seconds: Token lifetime in seconds from now (SPEC max: 86400).
        secret_key: HMAC signing key (from Settings.JWT_SECRET_KEY).
        algorithm: Signing algorithm string (e.g. ``"HS256"``).

    Returns:
        A 3-tuple ``(token, jti, exp)`` where:
        - ``token`` is the encoded JWT string.
        - ``jti`` is the unique token identifier (UUIDv4 string).
        - ``exp`` is the Unix timestamp (int) at which the token expires.

    Security: exp - iat == expires_seconds <= 86700 (SPEC tolerance).
    """
    jti: str = str(uuid.uuid4())
    iat: int = int(datetime.now(UTC).timestamp())
    exp: int = iat + expires_seconds

    payload: dict = {
        "sub": user_id,
        "jti": jti,
        "iat": iat,
        "exp": exp,
    }
    token: str = jwt.encode(payload, secret_key, algorithm=algorithm)
    return (token, jti, exp)


def decode_access_token(token: str, secret_key: str, algorithm: str) -> dict:
    """Decode and verify *token*, returning the payload dict.

    Args:
        token: The JWT string to decode.
        secret_key: HMAC key used for verification.
        algorithm: The expected algorithm (e.g. ``"HS256"``).

    Returns:
        The decoded payload dict with keys: ``sub``, ``jti``, ``iat``, ``exp``.

    Raises:
        jose.JWTError: If the token is expired, tampered, signed with the wrong
            key, or uses an algorithm not in the allowed list.

    Security: ``algorithms=[algorithm]`` (a list) forces python-jose to reject
    tokens with ``alg: none`` or any algorithm not explicitly allowed.
    """
    return jwt.decode(token, secret_key, algorithms=[algorithm])
