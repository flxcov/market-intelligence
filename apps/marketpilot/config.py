"""Application settings, loaded from environment variables and an optional `.env` file.

Environment variables take precedence over `.env`. Secrets are `SecretStr`, so
they never appear in `repr()`, logs, or tracebacks of the settings object.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from packages.database.engine import REQUIRED_URL_PREFIX
from packages.market_data.alpaca import DEFAULT_BASE_URL
from packages.market_data.models import DataFeed
from packages.market_data.options import OptionFeed


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    trading_mode: Literal["paper"] = "paper"

    market_data_provider: Literal["alpaca"] = "alpaca"
    alpaca_api_key: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None
    alpaca_data_base_url: str = DEFAULT_BASE_URL
    alpaca_data_feed: DataFeed = DataFeed.IEX
    # Option quotes: "indicative" is Alpaca's free feed, "opra" needs a subscription.
    alpaca_options_feed: OptionFeed = OptionFeed.INDICATIVE
    alpaca_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    alpaca_max_retries: int = Field(default=3, ge=0, le=10)

    # AlphaScout: SEC requires a User-Agent naming you and a contact email.
    sec_user_agent: str | None = Field(default=None, max_length=200)

    # Optional AI explanations (Anthropic). Unset = deterministic explanations only.
    anthropic_api_key: SecretStr | None = None
    ai_model: str = Field(default="claude-opus-5-5", min_length=1, max_length=64)
    ai_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    ai_max_retries: int = Field(default=2, ge=0, le=5)

    # e.g. postgresql+psycopg://user:password@localhost:5432/marketintel
    database_url: SecretStr | None = None
    # Bearer token required by state-changing endpoints. Unset = those endpoints are disabled.
    api_write_token: SecretStr | None = Field(default=None, min_length=16)

    @field_validator("trading_mode", mode="before")
    @classmethod
    def _paper_only(cls, value: object) -> object:
        if isinstance(value, str):
            if value.strip().lower() != "paper":
                raise ValueError(
                    "TRADING_MODE must be 'paper'. This project is for research and paper "
                    "trading only; live trading is not supported."
                )
            return "paper"
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator(
        "alpaca_api_key",
        "alpaca_secret_key",
        "anthropic_api_key",
        "database_url",
        "api_write_token",
        mode="before",
    )
    @classmethod
    def _blank_secret_is_missing(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("alpaca_data_base_url")
    @classmethod
    def _require_https(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith("https://"):
            raise ValueError("ALPACA_DATA_BASE_URL must use https://")
        return value

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        # Error messages deliberately never echo the URL: it contains a password.
        if not raw.startswith(REQUIRED_URL_PREFIX):
            raise ValueError(f"DATABASE_URL must start with '{REQUIRED_URL_PREFIX}'")
        try:
            url = make_url(raw)
        except ArgumentError:
            raise ValueError("DATABASE_URL is not a valid database URL") from None
        if not url.database:
            raise ValueError("DATABASE_URL must include a database name")
        return value

    def missing_alpaca_credentials(self) -> list[str]:
        missing = []
        if self.alpaca_api_key is None:
            missing.append("ALPACA_API_KEY")
        if self.alpaca_secret_key is None:
            missing.append("ALPACA_SECRET_KEY")
        return missing

    def secret_values(self) -> list[str]:
        """Raw secret values, used only to configure log redaction."""
        values = [
            s.get_secret_value()
            for s in (
                self.alpaca_api_key,
                self.alpaca_secret_key,
                self.anthropic_api_key,
                self.api_write_token,
            )
            if s is not None
        ]
        if self.database_url is not None:
            password = make_url(self.database_url.get_secret_value()).password
            if password:
                values.append(str(password))
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()
