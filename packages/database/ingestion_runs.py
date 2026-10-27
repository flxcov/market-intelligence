"""Repository for the ingestion audit log (`ingestion_runs`)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.market_bars import UpsertCounts
from packages.database.models import IngestionRunRecord, IngestionStatus
from packages.market_data.models import BarsRequest
from packages.shared.timeutils import utc_now

_MAX_ERROR_MESSAGE_CHARS = 2_000


async def start_run(session: AsyncSession, request: BarsRequest, *, provider: str) -> int:
    run = IngestionRunRecord(
        provider=provider,
        symbol=request.symbol,
        timeframe=request.timeframe,
        feed=request.feed.value if request.feed else None,
        adjustment=request.adjustment.value,
        range_start=request.start,
        range_end=request.end,
        status=IngestionStatus.RUNNING,
        started_at=utc_now(),
    )
    session.add(run)
    await session.flush()  # assigns run.id
    return run.id


async def mark_succeeded(
    session: AsyncSession,
    run_id: int,
    *,
    feed: str,
    bars_received: int,
    bars_skipped_incomplete: int,
    counts: UpsertCounts,
    truncated: bool,
) -> IngestionRunRecord:
    run = await session.get_one(IngestionRunRecord, run_id)
    run.status = IngestionStatus.SUCCEEDED
    run.finished_at = utc_now()
    run.feed = feed
    run.bars_received = bars_received
    run.bars_skipped_incomplete = bars_skipped_incomplete
    run.inserted = counts.inserted
    run.updated = counts.updated
    run.unchanged = counts.unchanged
    run.truncated = truncated
    await session.flush()
    return run


async def mark_failed(
    session: AsyncSession, run_id: int, *, error_type: str, error_message: str
) -> None:
    run = await session.get_one(IngestionRunRecord, run_id)
    run.status = IngestionStatus.FAILED
    run.finished_at = utc_now()
    run.error_type = error_type
    run.error_message = error_message[:_MAX_ERROR_MESSAGE_CHARS]
    await session.flush()


async def list_runs(
    session: AsyncSession, *, symbol: str | None = None, limit: int = 50
) -> list[IngestionRunRecord]:
    """Most recent runs first."""
    stmt = select(IngestionRunRecord).order_by(IngestionRunRecord.id.desc()).limit(limit)
    if symbol is not None:
        stmt = stmt.where(IngestionRunRecord.symbol == symbol)
    return list((await session.execute(stmt)).scalars().all())
