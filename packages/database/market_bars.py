"""Repository for stored market bars.

Idempotency: bars are written with PostgreSQL `INSERT ... ON CONFLICT` on the
natural key (symbol, timeframe, feed, adjustment, timestamp):
  - new key                      -> inserted (revision 1)
  - same key, identical values   -> untouched (counted as unchanged)
  - same key, different values   -> updated, revision + 1 (e.g. provider correction)
Re-running the same ingestion therefore never creates duplicates or churn.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import MarketBarRecord
from packages.market_data.models import Adjustment, Bar, DataFeed

UPSERT_BATCH_SIZE = 1_000  # keeps each statement well under PostgreSQL's parameter limit
_VALUE_COLUMNS = ("open", "high", "low", "close", "volume", "trade_count", "vwap")


@dataclass(frozen=True)
class SeriesKey:
    symbol: str
    timeframe: str
    feed: DataFeed
    adjustment: Adjustment


@dataclass(frozen=True)
class UpsertCounts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    def __add__(self, other: "UpsertCounts") -> "UpsertCounts":
        return UpsertCounts(
            self.inserted + other.inserted,
            self.updated + other.updated,
            self.unchanged + other.unchanged,
        )


async def upsert_bars(
    session: AsyncSession,
    key: SeriesKey,
    bars: Sequence[Bar],
    *,
    provider: str,
    retrieved_at: datetime,
) -> UpsertCounts:
    """Insert or update bars. Runs inside the caller's transaction; does not commit.

    `bars` must not contain two bars with the same timestamp (PostgreSQL rejects
    that within one statement); `normalize_bars` already guarantees this.
    """
    counts = UpsertCounts()
    for offset in range(0, len(bars), UPSERT_BATCH_SIZE):
        batch = bars[offset : offset + UPSERT_BATCH_SIZE]
        counts += await _upsert_batch(session, key, batch, provider, retrieved_at)
    return counts


async def _upsert_batch(
    session: AsyncSession,
    key: SeriesKey,
    bars: Sequence[Bar],
    provider: str,
    retrieved_at: datetime,
) -> UpsertCounts:
    rows = [
        {
            "symbol": key.symbol,
            "timeframe": key.timeframe,
            "feed": key.feed.value,
            "adjustment": key.adjustment.value,
            "timestamp": bar.timestamp,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "trade_count": bar.trade_count,
            "vwap": bar.vwap,
            "provider": provider,
            "retrieved_at": retrieved_at,
        }
        for bar in bars
    ]
    table = MarketBarRecord.__table__
    stmt = insert(table).values(rows)
    excluded = stmt.excluded
    values_changed = or_(*(table.c[col].is_distinct_from(excluded[col]) for col in _VALUE_COLUMNS))
    stmt = stmt.on_conflict_do_update(
        constraint="uq_market_bars_key",
        set_={
            **{col: excluded[col] for col in _VALUE_COLUMNS},
            "provider": excluded.provider,
            "retrieved_at": excluded.retrieved_at,
            "revision": table.c.revision + 1,
            "updated_at": func.now(),
        },
        where=values_changed,  # identical rows are skipped entirely
    ).returning(
        # PostgreSQL detail: xmax is 0 for a freshly inserted row version and
        # non-zero for a row rewritten by ON CONFLICT DO UPDATE.
        literal_column("xmax = 0").label("inserted")
    )
    written = (await session.execute(stmt)).scalars().all()
    inserted = sum(1 for flag in written if flag)
    updated = len(written) - inserted
    return UpsertCounts(inserted=inserted, updated=updated, unchanged=len(rows) - len(written))


async def get_bars(
    session: AsyncSession, key: SeriesKey, *, start: datetime, end: datetime, limit: int
) -> list[MarketBarRecord]:
    """Stored bars for one series with start <= timestamp <= end, oldest first."""
    stmt = (
        select(MarketBarRecord)
        .where(
            MarketBarRecord.symbol == key.symbol,
            MarketBarRecord.timeframe == key.timeframe,
            MarketBarRecord.feed == key.feed.value,
            MarketBarRecord.adjustment == key.adjustment.value,
            MarketBarRecord.timestamp >= start,
            MarketBarRecord.timestamp <= end,
        )
        .order_by(MarketBarRecord.timestamp)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_latest_bars(
    session: AsyncSession, key: SeriesKey, *, end: datetime, limit: int
) -> list[MarketBarRecord]:
    """The most recent `limit` stored bars with timestamp <= end, oldest first."""
    stmt = (
        select(MarketBarRecord)
        .where(
            MarketBarRecord.symbol == key.symbol,
            MarketBarRecord.timeframe == key.timeframe,
            MarketBarRecord.feed == key.feed.value,
            MarketBarRecord.adjustment == key.adjustment.value,
            MarketBarRecord.timestamp <= end,
        )
        .order_by(MarketBarRecord.timestamp.desc())
        .limit(limit)
    )
    return list(reversed((await session.execute(stmt)).scalars().all()))


async def get_bars_after(
    session: AsyncSession, key: SeriesKey, *, after: datetime, through: datetime
) -> list[MarketBarRecord]:
    """Stored bars with after < timestamp <= through, oldest first."""
    stmt = (
        select(MarketBarRecord)
        .where(
            MarketBarRecord.symbol == key.symbol,
            MarketBarRecord.timeframe == key.timeframe,
            MarketBarRecord.feed == key.feed.value,
            MarketBarRecord.adjustment == key.adjustment.value,
            MarketBarRecord.timestamp > after,
            MarketBarRecord.timestamp <= through,
        )
        .order_by(MarketBarRecord.timestamp)
    )
    return list((await session.execute(stmt)).scalars().all())


@dataclass(frozen=True)
class SeriesCoverage:
    symbol: str
    timeframe: str
    feed: str
    adjustment: str
    bars: int
    first_bar: datetime
    last_bar: datetime
    last_retrieved_at: datetime


async def coverage(session: AsyncSession, *, symbol: str | None = None) -> list[SeriesCoverage]:
    """What is stored: one row per series with its date range and freshness."""
    table = MarketBarRecord
    stmt = (
        select(
            table.symbol,
            table.timeframe,
            table.feed,
            table.adjustment,
            func.count(),
            func.min(table.timestamp),
            func.max(table.timestamp),
            func.max(table.retrieved_at),
        )
        .group_by(table.symbol, table.timeframe, table.feed, table.adjustment)
        .order_by(table.symbol, table.timeframe, table.feed, table.adjustment)
    )
    if symbol is not None:
        stmt = stmt.where(table.symbol == symbol)
    return [SeriesCoverage(*row) for row in (await session.execute(stmt)).all()]
