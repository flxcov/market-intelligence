from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from apps.marketpilot.api.dependencies import (
    get_app_settings,
    require_market_data_provider,
    require_session_factory,
)
from apps.marketpilot.api.schemas import (
    IngestionRun,
    SeriesCoverageView,
    StoredBar,
    StoredBarSeries,
)
from apps.marketpilot.api.security import require_write_access
from apps.marketpilot.config import Settings
from apps.marketpilot.services.ingestion import BarIngestionService, IngestionResult
from packages.database import ingestion_runs
from packages.database.market_bars import SeriesKey, coverage, get_bars
from packages.market_data.models import BarSeries, BarsRequest, Symbol

router = APIRouter(prefix="/market", tags=["market data"])

_STORED_NOTICE = (
    "Stored data: only bars that were ingested are returned. Gaps are possible where "
    "data was never ingested; completeness against a market calendar is not checked yet."
)


@router.get("/bars")
async def get_provider_bars(request: Request, query: Annotated[BarsRequest, Query()]) -> BarSeries:
    """Historical OHLCV bars fetched live from the configured provider (not stored).

    Prices are returned as decimal strings to preserve precision. Every
    response states its provider, feed, adjustment and retrieval time.
    """
    provider = require_market_data_provider(request)
    return await provider.get_bars(query)


@router.post("/bars/ingest", dependencies=[Depends(require_write_access)])
async def ingest_bars(request: Request, body: BarsRequest) -> IngestionResult:
    """Fetch bars from the provider and store them. Safe to repeat (idempotent).

    Requires `Authorization: Bearer <API_WRITE_TOKEN>`. Every attempt, including
    failures, is recorded and visible at `GET /market/ingestion-runs`.
    """
    service = BarIngestionService(
        require_market_data_provider(request), require_session_factory(request)
    )
    return await service.ingest(body)


@router.get("/bars/stored")
async def get_stored_bars(
    request: Request,
    query: Annotated[BarsRequest, Query()],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> StoredBarSeries:
    """Bars previously ingested into the database. Makes no provider call."""
    sessions = require_session_factory(request)
    feed = query.feed or settings.alpaca_data_feed
    key = SeriesKey(query.symbol, query.timeframe, feed, query.adjustment)
    async with sessions() as session:
        # Fetch one extra row to detect whether the result was truncated.
        records = await get_bars(
            session, key, start=query.start, end=query.end, limit=query.max_bars + 1
        )
    truncated = len(records) > query.max_bars
    bars = [StoredBar.model_validate(r) for r in records[: query.max_bars]]
    notices = [_STORED_NOTICE]
    if not bars:
        notices.append("No stored bars match. Ingest them first with POST /market/bars/ingest.")
    if truncated:
        notices.append(f"Result truncated at max_bars={query.max_bars}; more bars are stored.")
    return StoredBarSeries(
        symbol=query.symbol,
        timeframe=query.timeframe,
        feed=feed,
        adjustment=query.adjustment,
        start=query.start,
        end=query.end,
        bars=bars,
        truncated=truncated,
        notices=notices,
    )


@router.get("/ingestion-runs")
async def list_ingestion_runs(
    request: Request,
    symbol: Annotated[Symbol | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[IngestionRun]:
    """Recent ingestion attempts (newest first), including failures and their reasons."""
    sessions = require_session_factory(request)
    async with sessions() as session:
        runs = await ingestion_runs.list_runs(session, symbol=symbol, limit=limit)
    return [IngestionRun.model_validate(run) for run in runs]


@router.get("/coverage")
async def stored_coverage(
    request: Request, symbol: Annotated[Symbol | None, Query()] = None
) -> list[SeriesCoverageView]:
    """Stored series with their first/last bar and last retrieval time (data freshness)."""
    sessions = require_session_factory(request)
    async with sessions() as session:
        rows = await coverage(session, symbol=symbol)
    return [SeriesCoverageView.model_validate(r) for r in rows]
