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

    # NOTE (WR-01): EDGAR_USER_AGENT is intentionally NOT a Settings field.
    # app.services.edgar_client.EDGAR_USER_AGENT is the single source of truth
    # for the SEC-mandated User-Agent header, and it is read at MODULE IMPORT
    # time by the module-level `edgar_client` singleton. Sourcing it from
    # Settings here would require calling get_settings() during that import,
    # which eagerly validates DATABASE_URL/JWT_SECRET_KEY as well — breaking
    # every test/script that imports edgar_client (directly or transitively
    # via ingestion_service) without a full .env configured. If an
    # environment-level override becomes necessary, wire edgar_client to read
    # os.environ["EDGAR_USER_AGENT"] directly (with the current string as
    # fallback) rather than reintroducing a Settings field here.

    # ChromaDB — vector store (host-machine dev targets Docker-exposed port 8001)
    # In Docker network the api service overrides to host="chromadb" port=8000 via env
    CHROMADB_HOST: str = "localhost"
    CHROMADB_PORT: int = 8001
    CHROMADB_COLLECTION: str = "vantage_chunks"


def get_settings() -> Settings:
    """Return a Settings instance populated from environment / .env.

    Used as a FastAPI dependency: ``Depends(get_settings)``.
    Tests can override via ``app.dependency_overrides[get_settings]``.
    """
    return Settings()  # type: ignore[call-arg]
