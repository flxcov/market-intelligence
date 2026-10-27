"""MarketPilot API application factory.

Run with:  uvicorn apps.marketpilot.main:create_app --factory
Using a factory (instead of a module-level `app`) means importing this module
has no side effects, and tests can build apps with their own settings.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.marketpilot import __version__
from apps.marketpilot.api.errors import register_exception_handlers
from apps.marketpilot.api.routes import (
    alerts,
    backtests,
    deployments,
    explanations,
    health,
    market,
    options,
    paper,
    signals,
)
from apps.marketpilot.config import Settings, get_settings
from apps.marketpilot.market_data import build_market_data_provider
from packages.ai.model import AnthropicExplanationModel, ExplanationModel
from packages.database.engine import create_engine, create_session_factory
from packages.market_data.errors import ProviderConfigurationError
from packages.market_data.provider import MarketDataProvider
from packages.shared.logging_setup import configure_logging

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    provider: MarketDataProvider | None = None,
    explanation_model: ExplanationModel | None = None,
) -> FastAPI:
    """Build the app. Pass `provider` / `explanation_model` to inject fakes (tests)."""
    settings = settings or get_settings()
    configure_logging(
        level=settings.log_level, fmt=settings.log_format, secrets=settings.secret_values()
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned: MarketDataProvider | None = None
        app.state.market_data_error = None
        if provider is not None:
            app.state.market_data = provider
        else:
            try:
                owned = build_market_data_provider(settings)
            except ProviderConfigurationError as exc:
                # Start anyway so /health can explain what is missing.
                app.state.market_data_error = exc.message
                logger.warning("market data provider not configured", extra={"detail": exc.message})
            app.state.market_data = owned

        owned_ai: ExplanationModel | None = None
        if explanation_model is not None:
            app.state.explanation_model = explanation_model
        elif settings.anthropic_api_key is not None:
            owned_ai = AnthropicExplanationModel(
                api_key=settings.anthropic_api_key,
                model=settings.ai_model,
                timeout_seconds=settings.ai_timeout_seconds,
                max_retries=settings.ai_max_retries,
            )
            app.state.explanation_model = owned_ai

        # Creating the engine does not connect; /health reports reachability.
        engine = None
        if settings.database_url is not None:
            engine = create_engine(settings.database_url.get_secret_value())
            app.state.db_sessions = create_session_factory(engine)
        app.state.db_engine = engine

        logger.info(
            "marketpilot started",
            extra={
                "environment": settings.app_env,
                "trading_mode": settings.trading_mode,
                "market_data_configured": app.state.market_data is not None,
                "database_configured": engine is not None,
                "writes_enabled": settings.api_write_token is not None,
                "ai_explanations_enabled": app.state.explanation_model is not None,
            },
        )
        yield
        if owned is not None:
            await owned.aclose()
        if owned_ai is not None:
            await owned_ai.aclose()
        if engine is not None:
            await engine.dispose()

    app = FastAPI(
        title="MarketPilot AI",
        version=__version__,
        description=(
            "Market data, research and paper trading API. Paper trading only: "
            "this service has no live order execution."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.market_data = None
    app.state.market_data_error = None
    app.state.explanation_model = None
    app.state.db_engine = None
    app.state.db_sessions = None
    app.include_router(health.router)
    app.include_router(market.router)
    app.include_router(options.router)
    app.include_router(signals.router)
    app.include_router(paper.router)
    app.include_router(deployments.router)
    app.include_router(backtests.router)
    app.include_router(alerts.router)
    app.include_router(explanations.router)
    register_exception_handlers(app)
    return app
