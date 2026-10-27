"""Fetch bars from a provider and store them idempotently, with an audit trail.

Transaction design:
  1. Record the run as `running` and commit, so even a crash leaves a trace.
  2. Fetch from the provider with NO database transaction open (network calls
     can be slow; holding a transaction open would block other writers).
  3. Upsert bars and mark the run `succeeded` in ONE transaction, so stored
     bars and the audit record can never disagree.
  4. On any failure, mark the run `failed` in a fresh transaction and re-raise.

Only completed bars are stored: if the provider says the last bar's interval
had not ended at retrieval time, that bar is skipped (it would otherwise be
saved with values that are about to change).
"""

import logging
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.database import ingestion_runs
from packages.database.market_bars import SeriesKey, upsert_bars
from packages.database.models import IngestionStatus
from packages.market_data.models import Adjustment, BarsRequest, DataFeed
from packages.market_data.provider import MarketDataProvider
from packages.shared.errors import AppError

logger = logging.getLogger(__name__)


class IngestionResult(BaseModel):
    run_id: int
    status: IngestionStatus
    provider: str
    symbol: str
    timeframe: str
    feed: DataFeed
    adjustment: Adjustment
    start: datetime
    end: datetime
    retrieved_at: datetime
    bars_received: int
    bars_skipped_incomplete: int
    inserted: int
    updated: int
    unchanged: int
    truncated: bool
    notices: list[str]


class BarIngestionService:
    def __init__(
        self, provider: MarketDataProvider, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._provider = provider
        self._sessions = session_factory

    async def ingest(self, request: BarsRequest) -> IngestionResult:
        async with self._sessions.begin() as session:
            run_id = await ingestion_runs.start_run(session, request, provider=self._provider.name)

        try:
            return await self._fetch_and_store(run_id, request)
        except Exception as exc:
            await self._record_failure(run_id, exc)
            raise

    async def _fetch_and_store(self, run_id: int, request: BarsRequest) -> IngestionResult:
        series = await self._provider.get_bars(request)

        bars = series.bars
        skipped = 0
        if series.last_bar_may_be_incomplete:
            bars, skipped = bars[:-1], 1

        key = SeriesKey(series.symbol, series.timeframe, series.feed, series.adjustment)
        async with self._sessions.begin() as session:
            counts = await upsert_bars(
                session, key, bars, provider=series.provider, retrieved_at=series.retrieved_at
            )
            await ingestion_runs.mark_succeeded(
                session,
                run_id,
                feed=series.feed.value,
                bars_received=series.bar_count,
                bars_skipped_incomplete=skipped,
                counts=counts,
                truncated=series.truncated,
            )

        notices = list(series.notices)
        if skipped:
            notices.append("The last bar was not stored because its interval had not ended.")
        logger.info(
            "bars ingested",
            extra={
                "run_id": run_id,
                "symbol": series.symbol,
                "timeframe": series.timeframe,
                "feed": series.feed.value,
                "inserted": counts.inserted,
                "updated": counts.updated,
                "unchanged": counts.unchanged,
                "skipped_incomplete": skipped,
            },
        )
        return IngestionResult(
            run_id=run_id,
            status=IngestionStatus.SUCCEEDED,
            provider=series.provider,
            symbol=series.symbol,
            timeframe=series.timeframe,
            feed=series.feed,
            adjustment=series.adjustment,
            start=series.start,
            end=series.end,
            retrieved_at=series.retrieved_at,
            bars_received=series.bar_count,
            bars_skipped_incomplete=skipped,
            inserted=counts.inserted,
            updated=counts.updated,
            unchanged=counts.unchanged,
            truncated=series.truncated,
            notices=notices,
        )

    async def _record_failure(self, run_id: int, exc: Exception) -> None:
        if isinstance(exc, AppError):
            error_type, message = exc.error_type, exc.message
        else:
            # Unexpected errors: store only the class name; details go to the logs.
            error_type, message = "internal_error", type(exc).__name__
        try:
            async with self._sessions.begin() as session:
                await ingestion_runs.mark_failed(
                    session, run_id, error_type=error_type, error_message=message
                )
        except SQLAlchemyError:
            # The database itself may be what failed; the original error matters more.
            logger.exception("could not record failed ingestion run", extra={"run_id": run_id})
        logger.warning(
            "ingestion failed",
            extra={"run_id": run_id, "error_type": error_type},
            exc_info=not isinstance(exc, AppError),
        )
