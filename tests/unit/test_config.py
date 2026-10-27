from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.marketpilot.config import Settings
from apps.marketpilot.market_data import build_market_data_provider
from packages.market_data.alpaca import AlpacaMarketDataProvider
from packages.market_data.errors import ProviderConfigurationError
from packages.market_data.models import DataFeed

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_defaults_are_safe() -> None:
    settings = make_settings()
    assert settings.trading_mode == "paper"
    assert settings.alpaca_data_feed is DataFeed.IEX
    assert settings.alpaca_data_base_url == "https://data.alpaca.markets"
    assert settings.missing_alpaca_credentials() == ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"]


@pytest.mark.parametrize("mode", ["live", "LIVE", "real", ""])
def test_non_paper_trading_mode_is_rejected(mode: str) -> None:
    with pytest.raises(ValidationError, match="TRADING_MODE must be 'paper'"):
        make_settings(trading_mode=mode)


def test_trading_mode_from_env_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    assert make_settings().trading_mode == "paper"


def test_live_trading_mode_from_env_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(ValidationError):
        make_settings()


def test_blank_credentials_are_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "   ")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "")
    settings = make_settings()
    assert settings.alpaca_api_key is None
    assert settings.missing_alpaca_credentials() == ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"]


def test_partial_credentials_report_only_the_missing_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-id-123")
    assert make_settings().missing_alpaca_credentials() == ["ALPACA_SECRET_KEY"]


def test_secrets_are_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-id-123")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret-456")
    settings = make_settings()
    assert "test-key-id-123" not in repr(settings)
    assert "test-secret-456" not in str(settings.model_dump())
    assert settings.secret_values() == ["test-key-id-123", "test-secret-456"]


def test_base_url_must_be_https() -> None:
    with pytest.raises(ValidationError, match="https"):
        make_settings(alpaca_data_base_url="http://data.alpaca.markets")
    assert (
        make_settings(alpaca_data_base_url="https://example.test/").alpaca_data_base_url
        == "https://example.test"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("alpaca_timeout_seconds", 0), ("alpaca_max_retries", -1), ("alpaca_data_feed", "otc")],
)
def test_out_of_range_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_env_file_is_loaded(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ALPACA_API_KEY=file-key-123\nALPACA_SECRET_KEY=file-secret-456\nLOG_LEVEL=debug\n"
    )
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert settings.missing_alpaca_credentials() == []
    assert settings.log_level == "DEBUG"


def test_env_example_parses_and_has_no_credentials() -> None:
    settings = Settings(_env_file=PROJECT_ROOT / ".env.example")  # type: ignore[call-arg]
    assert settings.trading_mode == "paper"
    assert settings.alpaca_data_feed is DataFeed.IEX
    assert settings.missing_alpaca_credentials() == ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"]


def test_build_provider_without_credentials_raises_clear_error() -> None:
    with pytest.raises(ProviderConfigurationError, match="ALPACA_API_KEY, ALPACA_SECRET_KEY"):
        build_market_data_provider(make_settings())


@pytest.mark.anyio
async def test_build_provider_with_credentials() -> None:
    provider = build_market_data_provider(
        make_settings(alpaca_api_key="test-key-id-123", alpaca_secret_key="test-secret-456")
    )
    assert isinstance(provider, AlpacaMarketDataProvider)
    await provider.aclose()


# --- database & write token (Phase 2) ------------------------------------------

DB_URL = "postgresql+psycopg://marketintel:db-pass-987@localhost:5432/marketintel"


def test_database_url_is_optional_and_secret() -> None:
    assert make_settings().database_url is None
    settings = make_settings(database_url=DB_URL)
    assert "db-pass-987" not in repr(settings)
    assert "db-pass-987" in settings.secret_values()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://marketintel:db-pass-987@localhost/marketintel",  # missing +psycopg
        "sqlite:///local.db",
        "postgresql+psycopg://marketintel:db-pass-987@localhost:5432",  # no database name
    ],
)
def test_invalid_database_urls_are_rejected_without_echoing_them(url: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        make_settings(database_url=url)
    assert "db-pass-987" not in str(exc_info.value)


def test_blank_database_url_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "  ")
    assert make_settings().database_url is None


def test_write_token_must_be_long_enough() -> None:
    with pytest.raises(ValidationError):
        make_settings(api_write_token="short")
    settings = make_settings(api_write_token="a-long-enough-token-123")
    assert "a-long-enough-token-123" in settings.secret_values()


def test_anthropic_key_is_optional_secret_and_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", " ")
    assert make_settings().anthropic_api_key is None
    settings = make_settings(anthropic_api_key="sk-ant-test-123456")
    assert "sk-ant-test-123456" in settings.secret_values()
    assert "sk-ant-test-123456" not in repr(settings)
    assert settings.ai_model == "claude-opus-5-5"
