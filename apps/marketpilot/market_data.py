"""Builds the configured market data provider from settings."""

from apps.marketpilot.config import Settings
from packages.market_data.alpaca import AlpacaMarketDataProvider
from packages.market_data.errors import ProviderConfigurationError
from packages.market_data.provider import MarketDataProvider


def build_market_data_provider(settings: Settings) -> MarketDataProvider:
    api_key, secret_key = settings.alpaca_api_key, settings.alpaca_secret_key
    if api_key is None or secret_key is None:
        missing = ", ".join(settings.missing_alpaca_credentials())
        raise ProviderConfigurationError(
            f"Market data provider 'alpaca' is not configured: missing {missing}. "
            "Set these in your .env file (see .env.example) and restart the server.",
            provider="alpaca",
        )
    return AlpacaMarketDataProvider.create(
        api_key=api_key,
        secret_key=secret_key,
        base_url=settings.alpaca_data_base_url,
        timeout_seconds=settings.alpaca_timeout_seconds,
        default_feed=settings.alpaca_data_feed,
        max_retries=settings.alpaca_max_retries,
    )
