"""Application configuration via pydantic-settings.

Settings are loaded from environment variables and optionally from a .env file.
Required fields (DATABASE_URL, JWT_SECRET_KEY) raise ValidationError at startup
if absent — no insecure defaults allowed (T-01-02-01).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database — required; no default (fails fast at startup)
    DATABASE_URL: str

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT — required; no default (fails fast at startup)
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_SECONDS: int = 86400  # 24 hours

    # EDGAR policy (User-Agent required on every request)
    EDGAR_USER_AGENT: str = "Vantage/1.0 vishwak.vel@gmail.com"


def get_settings() -> Settings:
    """Return a Settings instance populated from environment / .env.

    Used as a FastAPI dependency: ``Depends(get_settings)``.
    Tests can override via ``app.dependency_overrides[get_settings]``.
    """
    return Settings()  # type: ignore[call-arg]
