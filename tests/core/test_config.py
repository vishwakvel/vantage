"""Tests for ALLOWED_ORIGINS parsing behaviour (D-11, DEPLOY-02).

Settings is constructed directly with explicit keyword arguments, the same
way tests/conftest.py's test_settings fixture does, so these tests do not
depend on any .env file or ambient environment state.
"""

from app.core.config import Settings, parse_allowed_origins

REQUIRED_FIELDS = {
    "DATABASE_URL": "postgresql+asyncpg://vantage:vantage@localhost:5433/vantage_test",
    "JWT_SECRET_KEY": "test-jwt-secret-not-for-production",
    "GROQ_API_KEY": "test-groq-key-not-for-production",
}


def test_default_origin_is_vite_dev_server() -> None:
    """With no override, the setting equals the Vite dev server address."""
    settings = Settings(**REQUIRED_FIELDS)
    assert settings.ALLOWED_ORIGINS == "http://localhost:5173"
    assert parse_allowed_origins(settings.ALLOWED_ORIGINS) == ["http://localhost:5173"]


def test_single_origin_parses_to_one_element_list() -> None:
    """A single configured origin parses to a one-element list containing it."""
    settings = Settings(**REQUIRED_FIELDS, ALLOWED_ORIGINS="https://vantage.example.com")
    assert parse_allowed_origins(settings.ALLOWED_ORIGINS) == ["https://vantage.example.com"]


def test_two_origins_parse_in_order() -> None:
    """Two comma-separated origins parse to exactly two elements, in order."""
    settings = Settings(
        **REQUIRED_FIELDS,
        ALLOWED_ORIGINS="https://a.example.com,https://b.example.com",
    )
    assert parse_allowed_origins(settings.ALLOWED_ORIGINS) == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_trailing_comma_does_not_inject_empty_origin() -> None:
    """A trailing comma must not produce an empty-string entry (T-14-24)."""
    settings = Settings(**REQUIRED_FIELDS, ALLOWED_ORIGINS="https://a.example.com,")
    parsed = parse_allowed_origins(settings.ALLOWED_ORIGINS)
    assert parsed == ["https://a.example.com"]
    assert "" not in parsed


def test_doubled_comma_does_not_inject_empty_origin() -> None:
    """A doubled comma must not produce an empty-string entry (T-14-24)."""
    settings = Settings(
        **REQUIRED_FIELDS,
        ALLOWED_ORIGINS="https://a.example.com,,https://b.example.com",
    )
    parsed = parse_allowed_origins(settings.ALLOWED_ORIGINS)
    assert parsed == ["https://a.example.com", "https://b.example.com"]
    assert "" not in parsed


def test_surrounding_whitespace_is_stripped() -> None:
    """Whitespace around each origin is stripped from every element."""
    settings = Settings(
        **REQUIRED_FIELDS,
        ALLOWED_ORIGINS=" https://a.example.com , https://b.example.com ",
    )
    parsed = parse_allowed_origins(settings.ALLOWED_ORIGINS)
    assert parsed == ["https://a.example.com", "https://b.example.com"]
    assert all(origin == origin.strip() for origin in parsed)
