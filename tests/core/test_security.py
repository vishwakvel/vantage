"""Unit tests for app.core.security — RED phase.

Tests cover:
- hash_password: bcrypt prefix, not plaintext
- verify_password: correct and wrong passwords
- create_access_token: 3-tuple shape, exp-iat tolerance
- decode_access_token: payload keys, tampered token rejection, "none" algorithm rejection
"""

import time

import pytest
from jose import JWTError


class TestHashPassword:
    """hash_password produces bcrypt hashes."""

    def test_returns_bcrypt_prefix(self):
        from app.core.security import hash_password

        h = hash_password("mysecret")
        assert h.startswith("$2b$"), f"Expected bcrypt hash starting with $2b$, got: {h[:10]}"

    def test_not_plaintext(self):
        from app.core.security import hash_password

        h = hash_password("mysecret")
        assert h != "mysecret"

    def test_different_salts_each_call(self):
        from app.core.security import hash_password

        h1 = hash_password("same-password")
        h2 = hash_password("same-password")
        # bcrypt generates a new salt each call — hashes must differ
        assert h1 != h2


class TestVerifyPassword:
    """verify_password returns True/False correctly."""

    def test_correct_password_returns_true(self):
        from app.core.security import hash_password, verify_password

        h = hash_password("correct")
        assert verify_password("correct", h) is True

    def test_wrong_password_returns_false(self):
        from app.core.security import hash_password, verify_password

        h = hash_password("correct")
        assert verify_password("wrong", h) is False

    def test_empty_password_wrong(self):
        from app.core.security import hash_password, verify_password

        h = hash_password("notempty")
        assert verify_password("", h) is False


class TestCreateAccessToken:
    """create_access_token returns (token, jti, exp) 3-tuple."""

    SECRET = "test-secret-key"
    ALG = "HS256"

    def test_returns_3_tuple(self):
        from app.core.security import create_access_token

        result = create_access_token("user-123", 86400, self.SECRET, self.ALG)
        assert len(result) == 3, f"Expected 3-tuple, got length {len(result)}"

    def test_token_is_string(self):
        from app.core.security import create_access_token

        token, jti, exp = create_access_token("user-123", 86400, self.SECRET, self.ALG)
        assert isinstance(token, str)

    def test_jti_is_string(self):
        from app.core.security import create_access_token

        token, jti, exp = create_access_token("user-123", 86400, self.SECRET, self.ALG)
        assert isinstance(jti, str)

    def test_exp_is_int(self):
        from app.core.security import create_access_token

        token, jti, exp = create_access_token("user-123", 86400, self.SECRET, self.ALG)
        assert isinstance(exp, int)

    def test_exp_within_tolerance(self):
        """exp - now must be <= 86700 (24h + 5 min SPEC tolerance)."""
        from app.core.security import create_access_token

        now = int(time.time())
        token, jti, exp = create_access_token("user-123", 86400, self.SECRET, self.ALG)
        assert exp - now <= 86700, f"exp={exp} - now={now} = {exp - now} exceeds 86700"

    def test_jti_unique_per_call(self):
        from app.core.security import create_access_token

        _, jti1, _ = create_access_token("user-123", 86400, self.SECRET, self.ALG)
        _, jti2, _ = create_access_token("user-123", 86400, self.SECRET, self.ALG)
        assert jti1 != jti2


class TestDecodeAccessToken:
    """decode_access_token validates and decodes JWT."""

    SECRET = "test-secret-key"
    ALG = "HS256"

    def _make_token(self):
        from app.core.security import create_access_token

        return create_access_token("user-abc", 86400, self.SECRET, self.ALG)

    def test_returns_dict_with_required_keys(self):
        from app.core.security import decode_access_token

        token, jti, exp = self._make_token()
        payload = decode_access_token(token, self.SECRET, self.ALG)
        assert isinstance(payload, dict)
        for key in ("sub", "jti", "iat", "exp"):
            assert key in payload, f"Missing key: {key}"

    def test_sub_matches_user_id(self):
        from app.core.security import decode_access_token

        token, jti, exp = self._make_token()
        payload = decode_access_token(token, self.SECRET, self.ALG)
        assert payload["sub"] == "user-abc"

    def test_jti_matches(self):
        from app.core.security import create_access_token, decode_access_token

        token, jti, exp = create_access_token("user-abc", 86400, self.SECRET, self.ALG)
        payload = decode_access_token(token, self.SECRET, self.ALG)
        assert payload["jti"] == jti

    def test_tampered_token_raises_jwtError(self):
        from app.core.security import decode_access_token

        token, _, _ = self._make_token()
        # Corrupt the signature by appending characters
        tampered = token[:-4] + "XXXX"
        with pytest.raises(JWTError):
            decode_access_token(tampered, self.SECRET, self.ALG)

    def test_wrong_secret_raises_jwtError(self):
        from app.core.security import decode_access_token

        token, _, _ = self._make_token()
        with pytest.raises(JWTError):
            decode_access_token(token, "wrong-secret", self.ALG)

    def test_none_algorithm_rejected(self):
        """Passing 'none' as algorithm must raise JWTError — algorithms list enforced."""
        from app.core.security import decode_access_token

        token, _, _ = self._make_token()
        with pytest.raises((JWTError, Exception)):
            # "none" is not in the allowed list; jose should reject it
            decode_access_token(token, self.SECRET, "none")
