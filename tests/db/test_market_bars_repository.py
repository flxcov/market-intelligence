from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.database import market_bars
from packages.database.market_bars import SeriesKey, UpsertCounts, get_bars, upsert_bars
from packages.database.models import MarketBarRecord
from packages.market_data.models import Adjustment, Bar, DataFeed
from tests.fakes import synthetic_daily_bars

pytestmark = pytest.mark.anyio

KEY = SeriesKey("TEST", "1Day", DataFeed.IEX, Adjustment.RAW)
T1 = datetime(2024, 2, 1, tzinfo=UTC)
T2 = datetime(2024, 2, 2, tzinfo=UTC)

Sessions = async_sessionmaker[AsyncSession]


async def upsert(sessions: Sessions, bars: list[Bar], *, key: SeriesKey = KEY, at: datetime = T1):
    async with sessions.begin() as session:
        return await upsert_bars(session, key, bars, provider="fake", retrieved_at=at)


async def stored(sessions: Sessions, key: SeriesKey = KEY) -> list[MarketBarRecord]:
    async with sessions() as session:
        return await get_bars(
            session,
            key,
            start=datetime(2000, 1, 1, tzinfo=UTC),
            end=datetime(2100, 1, 1, tzinfo=UTC),
            limit=10_000,
        )


async def test_first_write_inserts(sessions: Sessions) -> None:
    counts = await upsert(sessions, synthetic_daily_bars(3))
    assert counts == UpsertCounts(inserted=3, updated=0, unchanged=0)
    assert len(await stored(sessions)) == 3


async def test_repeating_the_same_write_changes_nothing(sessions: Sessions) -> None:
    bars = synthetic_daily_bars(3)
    await upsert(sessions, bars, at=T1)
    before = await stored(sessions)

    counts = await upsert(sessions, bars, at=T2)

    assert counts == UpsertCounts(inserted=0, updated=0, unchanged=3)
    after = await stored(sessions)
    assert [(r.id, r.revision, r.retrieved_at, r.updated_at) for r in after] == [
        (r.id, r.revision, r.retrieved_at, r.updated_at) for r in before
    ]


async def test_equal_decimals_with_different_scale_are_unchanged(sessions: Sessions) -> None:
    bar = synthetic_daily_bars(1)[0]
    await upsert(sessions, [bar])
    rescaled = bar.model_copy(update={"open": bar.open.quantize(Decimal("0.0001"))})
    assert str(rescaled.open) != str(bar.open)

    assert await upsert(sessions, [rescaled]) == UpsertCounts(unchanged=1)


async def test_changed_values_update_and_bump_revision(sessions: Sessions) -> None:
    bars = synthetic_daily_bars(2)
    await upsert(sessions, bars, at=T1)
    corrected = bars[1].model_copy(update={"close": bars[1].close + Decimal("0.01")})

    counts = await upsert(sessions, [bars[0], corrected], at=T2)

    assert counts == UpsertCounts(inserted=0, updated=1, unchanged=1)
    first, second = await stored(sessions)
    assert (first.revision, first.retrieved_at) == (1, T1)
    assert (second.revision, second.retrieved_at) == (2, T2)
    assert second.close == corrected.close


async def test_mixed_batch_counts(sessions: Sessions) -> None:
    bars = synthetic_daily_bars(3)
    await upsert(sessions, bars[:2])
    changed = bars[1].model_copy(update={"volume": Decimal("1")})

    counts = await upsert(sessions, [bars[0], changed, bars[2]])

    assert counts == UpsertCounts(inserted=1, updated=1, unchanged=1)


async def test_decimal_values_round_trip_exactly(sessions: Sessions) -> None:
    bar = Bar(
        timestamp=datetime(2024, 1, 2, 5, tzinfo=UTC),
        open=Decimal("0.000123456789"),
        high=Decimal("0.000123456790"),
        low=Decimal("0.000123456788"),
        close=Decimal("0.000123456789"),
        volume=Decimal("123456789012.5"),
        vwap=Decimal("0.0001234567891234"),
    )
    await upsert(sessions, [bar])
    (record,) = await stored(sessions)
    assert (record.open, record.high, record.volume, record.vwap) == (
        bar.open,
        bar.high,
        bar.volume,
        bar.vwap,
    )
    assert record.timestamp == bar.timestamp
    assert record.timestamp.utcoffset() == timedelta(0)


async def test_series_are_isolated_by_feed_and_adjustment(sessions: Sessions) -> None:
    bars = synthetic_daily_bars(2)
    other_feed = SeriesKey("TEST", "1Day", DataFeed.SIP, Adjustment.RAW)
    other_adjustment = SeriesKey("TEST", "1Day", DataFeed.IEX, Adjustment.ALL)
    for key in (KEY, other_feed, other_adjustment):
        assert (await upsert(sessions, bars, key=key)).inserted == 2

    for key in (KEY, other_feed, other_adjustment):
        assert len(await stored(sessions, key)) == 2


async def test_large_writes_are_batched(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(market_bars, "UPSERT_BATCH_SIZE", 2)
    bars = synthetic_daily_bars(5)

    assert await upsert(sessions, bars) == UpsertCounts(inserted=5)
    assert await upsert(sessions, bars) == UpsertCounts(unchanged=5)


async def test_get_bars_filters_range_inclusively_and_orders(sessions: Sessions) -> None:
    bars = synthetic_daily_bars(5)  # 2024-01-02 .. 2024-01-06, 05:00Z
    await upsert(sessions, list(reversed(bars)))

    async with sessions() as session:
        records = await get_bars(
            session, KEY, start=bars[1].timestamp, end=bars[3].timestamp, limit=100
        )
        limited = await get_bars(
            session, KEY, start=bars[0].timestamp, end=bars[4].timestamp, limit=2
        )

    assert [r.timestamp for r in records] == [b.timestamp for b in bars[1:4]]
    assert [r.timestamp for r in limited] == [b.timestamp for b in bars[:2]]


async def test_failed_transaction_writes_nothing(sessions: Sessions) -> None:
    with pytest.raises(RuntimeError):
        async with sessions.begin() as session:
            await upsert_bars(
                session, KEY, synthetic_daily_bars(2), provider="fake", retrieved_at=T1
            )
            raise RuntimeError("simulated failure after the upsert")

    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MarketBarRecord)) == 0
