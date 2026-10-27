"""AlphaScout API: evidence-based research support. Run separately from MarketPilot:

    uvicorn apps.alphascout.main:create_app --factory --port 8001

Shares the database and settings with MarketPilot. Nothing here can place orders.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from pydantic import BaseModel

from apps.alphascout.analysis.report import ENGINE_VERSION
from apps.alphascout.api.routes import router
from apps.marketpilot.api.errors import register_exception_handlers
from apps.marketpilot.config import Settings, get_settings
from apps.marketpilot.market_data import build_market_data_provider
from packages.database.engine import create_engine, create_session_factory
from packages.market_data.provider import MarketDataProvider
from packages.sec.client import SecEdgarClient
from packages.sec.errors import SecConfigurationError
from packages.shared.logging_setup import configure_logging
from packages.shared.timeutils import utc_now

logger = logging.getLogger(__name__)


class AlphaScoutHealth(BaseModel):
    status: str
    service: str = "alphascout"
    engine_version: str = ENGINE_VERSION
    sec_configured: bool
    sec_detail: str | None
    news_configured: bool
    database_configured: bool
    writes_enabled: bool
    time_utc: datetime


def create_app(
    settings: Settings | None = None,
    sec: SecEdgarClient | None = None,
    news: MarketDataProvider | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    secrets = settings.secret_values()
    if settings.sec_user_agent:
        secrets.append(settings.sec_user_agent)  # contains a contact email
    configure_logging(level=settings.log_level, fmt=settings.log_format, secrets=secrets)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = None
        if sec is not None:
            app.state.sec = sec
        elif settings.sec_user_agent:
            try:
                owned = SecEdgarClient.create(user_agent=settings.sec_user_agent)
                app.state.sec = owned
            except SecConfigurationError as exc:
                app.state.sec_error = exc.message
        else:
            app.state.sec_error = "SEC_USER_AGENT is not set (see .env.example)."
        owned_news = None
        if news is not None:
            app.state.news = news
        elif not settings.missing_alpaca_credentials():
            owned_news = build_market_data_provider(settings)  # Alpaca News API, same keys
            app.state.news = owned_news
        engine = None
        if settings.database_url is not None:
            engine = create_engine(settings.database_url.get_secret_value())
            app.state.db_sessions = create_session_factory(engine)
        yield
        if owned is not None:
            await owned.aclose()
        if owned_news is not None:
            await owned_news.aclose()
        if engine is not None:
            await engine.dispose()

    app = FastAPI(
        title="AlphaScout AI",
        description="Evidence-based research support. Not investment advice; no trading.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.sec = None
    app.state.sec_error = None
    app.state.news = None
    app.state.db_sessions = None

    @app.get("/health")
    async def health(request: Request) -> AlphaScoutHealth:
        configured = request.app.state.sec is not None
        database = request.app.state.db_sessions is not None
        return AlphaScoutHealth(
            status="ok" if configured and database else "degraded",
            sec_configured=configured,
            sec_detail=request.app.state.sec_error,
            news_configured=request.app.state.news is not None,
            database_configured=database,
            writes_enabled=settings.api_write_token is not None,
            time_utc=utc_now(),
        )

    app.include_router(router)
    register_exception_handlers(app)
    return app
