"""Pydantic request/response models for authentication endpoints.

Security constraints (SPEC prohibitions):
- TokenResponse deliberately omits any password or hash field — no credential
  is ever serialised into an API response.
- UserOut is built with from_attributes=True for ORM → schema conversion;
  password_hash column from the User ORM model is excluded by design.
"""

import uuid

from pydantic import BaseModel, EmailStr


class RegisterRequest(BaseModel):
    """Request body for POST /auth/register."""

    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    """Request body for POST /auth/login."""

    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    """Response body returned on successful register or login.

    Fields are intentionally minimal — no password, hash, or internal ID.
    """

    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    """Safe user representation for API responses.

    Only exposes id and email — never password_hash or any credential field.
    ``from_attributes=True`` enables direct construction from SQLAlchemy ORM objects.
    """

    id: uuid.UUID
    email: str

    model_config = {"from_attributes": True}
