"""Helpers that hand route handlers their collaborators.

The `require_*` helpers are called inside handlers rather than via `Depends()`:
FastAPI resolves dependencies before validating query parameters, and we want
invalid input reported as 422 even when a backend is not configured.
"""

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.config import Settings
from packages.database.errors import DatabaseNotConfiguredError
from packages.market_data.errors import ProviderConfigurationError
from packages.market_data.provider import MarketDataProvider


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def require_market_data_provider(request: Request) -> MarketDataProvider:
    provider: MarketDataProvider | None = request.app.state.market_data
    if provider is None:
        raise ProviderConfigurationError(
            request.app.state.market_data_error,
            provider=request.app.state.settings.market_data_provider,
        )
    return provider


def require_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    sessions: async_sessionmaker[AsyncSession] | None = request.app.state.db_sessions
    if sessions is None:
        raise DatabaseNotConfiguredError(
            "Database is not configured: set DATABASE_URL in .env (see .env.example), "
            "run `alembic upgrade head`, and restart the server."
        )
    return sessions
