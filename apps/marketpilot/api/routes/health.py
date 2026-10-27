from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from apps.marketpilot import __version__
from apps.marketpilot.api.dependencies import get_app_settings
from apps.marketpilot.config import Settings
from packages.database.engine import check_database
from packages.shared.timeutils import utc_now

router = APIRouter(tags=["health"])


class MarketDataStatus(BaseModel):
    provider: str
    configured: bool
    detail: str | None = None


class DatabaseHealth(BaseModel):
    configured: bool
    reachable: bool
    schema_revision: str | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    environment: str
    trading_mode: Literal["paper"]
    live_trading_enabled: Literal[False]
    writes_enabled: bool
    ai_explanations_enabled: bool
    time_utc: datetime
    market_data: MarketDataStatus
    database: DatabaseHealth


@router.get("/health")
async def health(
    request: Request, settings: Annotated[Settings, Depends(get_app_settings)]
) -> HealthResponse:
    """Service status. Checks database connectivity (2s timeout); never calls the provider."""
    market_data_configured = request.app.state.market_data is not None

    engine = request.app.state.db_engine
    if engine is None:
        database = DatabaseHealth(
            configured=False, reachable=False, detail="DATABASE_URL is not set."
        )
    else:
        db_status = await check_database(engine)
        database = DatabaseHealth(
            configured=True,
            reachable=db_status.reachable,
            schema_revision=db_status.schema_revision,
            detail=db_status.detail,
        )

    healthy = market_data_configured and database.schema_revision is not None
    return HealthResponse(
        status="ok" if healthy else "degraded",
        service="marketpilot",
        version=__version__,
        environment=settings.app_env,
        trading_mode=settings.trading_mode,
        live_trading_enabled=False,
        writes_enabled=settings.api_write_token is not None,
        ai_explanations_enabled=request.app.state.explanation_model is not None,
        time_utc=utc_now(),
        market_data=MarketDataStatus(
            provider=settings.market_data_provider,
            configured=market_data_configured,
            detail=request.app.state.market_data_error,
        ),
        database=database,
    )
