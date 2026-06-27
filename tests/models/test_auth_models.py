"""Unit tests for app.models.auth Pydantic models — RED phase.

Tests cover:
- RegisterRequest: email, password fields accepted
- LoginRequest: email, password fields accepted
- TokenResponse: access_token (str), token_type default "bearer", no password/hash fields
- UserOut: id (UUID), email (str), no password/hash fields, from_attributes config
"""

import uuid

import pytest
from pydantic import ValidationError


class TestRegisterRequest:
    """RegisterRequest model validation."""

    def test_accepts_valid_email_and_password(self):
        from app.models.auth import RegisterRequest

        r = RegisterRequest(email="user@example.com", password="s3cur3!")
        assert r.email == "user@example.com"
        assert r.password == "s3cur3!"

    def test_rejects_invalid_email(self):
        from app.models.auth import RegisterRequest

        with pytest.raises(ValidationError):
            RegisterRequest(email="not-an-email", password="pass")

    def test_has_email_field(self):
        from app.models.auth import RegisterRequest

        assert "email" in RegisterRequest.model_fields

    def test_has_password_field(self):
        from app.models.auth import RegisterRequest

        assert "password" in RegisterRequest.model_fields


class TestLoginRequest:
    """LoginRequest model validation."""

    def test_accepts_valid_email_and_password(self):
        from app.models.auth import LoginRequest

        r = LoginRequest(email="user@example.com", password="pass123")
        assert r.email == "user@example.com"
        assert r.password == "pass123"

    def test_rejects_invalid_email(self):
        from app.models.auth import LoginRequest

        with pytest.raises(ValidationError):
            LoginRequest(email="bad", password="pass")


class TestTokenResponse:
    """TokenResponse has access_token and token_type only."""

    def test_default_token_type_is_bearer(self):
        from app.models.auth import TokenResponse

        t = TokenResponse(access_token="some.jwt.token")
        assert t.token_type == "bearer"

    def test_access_token_field_exists(self):
        from app.models.auth import TokenResponse

        assert "access_token" in TokenResponse.model_fields

    def test_no_password_field(self):
        from app.models.auth import TokenResponse

        assert "password" not in TokenResponse.model_fields

    def test_no_password_hash_field(self):
        from app.models.auth import TokenResponse

        assert "password_hash" not in TokenResponse.model_fields

    def test_only_two_fields(self):
        from app.models.auth import TokenResponse

        assert set(TokenResponse.model_fields.keys()) == {"access_token", "token_type"}


class TestUserOut:
    """UserOut has id and email only — no password fields."""

    def test_accepts_uuid_id_and_email(self):
        from app.models.auth import UserOut

        uid = uuid.uuid4()
        u = UserOut(id=uid, email="user@example.com")
        assert u.id == uid
        assert u.email == "user@example.com"

    def test_no_password_field(self):
        from app.models.auth import UserOut

        assert "password" not in UserOut.model_fields

    def test_no_password_hash_field(self):
        from app.models.auth import UserOut

        assert "password_hash" not in UserOut.model_fields

    def test_from_attributes_config(self):
        """UserOut must have from_attributes=True for ORM model construction."""
        from app.models.auth import UserOut

        # Pydantic v2 model_config
        config = UserOut.model_config
        assert config.get("from_attributes") is True
