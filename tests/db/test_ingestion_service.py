from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.services.ingestion import BarIngestionService
from packages.database.models import IngestionRunRecord, IngestionStatus, MarketBarRecord
from packages.market_data.errors import ProviderAccessError
from packages.market_data.models import BarsRequest
from tests.fakes import FakeProvider, synthetic_daily_bars

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]
REQUEST = BarsRequest(symbol="TEST", start="2024-01-01", end="2024-01-31")


async def all_runs(sessions: Sessions) -> list[IngestionRunRecord]:
    async with sessions() as session:
        return list((await session.scalars(select(IngestionRunRecord))).all())


async def all_bars(sessions: Sessions) -> list[MarketBarRecord]:
    async with sessions() as session:
        stmt = select(MarketBarRecord).order_by(MarketBarRecord.timestamp)
        return list((await session.scalars(stmt)).all())


async def test_ingest_stores_bars_and_records_run(sessions: Sessions) -> None:
    service = BarIngestionService(FakeProvider(synthetic_daily_bars(3)), sessions)

    result = await service.ingest(REQUEST)

    assert (result.inserted, result.updated, result.unchanged) == (3, 0, 0)
    assert result.status is IngestionStatus.SUCCEEDED
    assert len(await all_bars(sessions)) == 3
    (run,) = await all_runs(sessions)
    assert run.id == result.run_id
    assert run.status == "succeeded"
    assert (run.feed, run.bars_received, run.inserted) == ("iex", 3, 3)
    assert run.finished_at is not None and run.finished_at >= run.started_at


async def test_repeated_ingest_is_idempotent(sessions: Sessions) -> None:
    service = BarIngestionService(FakeProvider(synthetic_daily_bars(3)), sessions)
    await service.ingest(REQUEST)

    second = await service.ingest(REQUEST)

    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 3)
    assert len(await all_bars(sessions)) == 3
    assert len(await all_runs(sessions)) == 2  # every attempt is audited


async def test_possibly_incomplete_last_bar_is_not_stored(sessions: Sessions) -> None:
    bars = synthetic_daily_bars(3)
    provider = FakeProvider(bars, last_bar_may_be_incomplete=True)

    result = await BarIngestionService(provider, sessions).ingest(REQUEST)

    assert (result.bars_received, result.bars_skipped_incomplete, result.inserted) == (3, 1, 2)
    stored = await all_bars(sessions)
    assert [b.timestamp for b in stored] == [b.timestamp for b in bars[:2]]
    assert any("not stored" in notice for notice in result.notices)


async def test_provider_failure_is_recorded_and_reraised(sessions: Sessions) -> None:
    error = ProviderAccessError("subscription does not permit this feed", provider="fake")
    service = BarIngestionService(FakeProvider(error=error), sessions)

    with pytest.raises(ProviderAccessError):
        await service.ingest(REQUEST)

    (run,) = await all_runs(sessions)
    assert run.status == "failed"
    assert run.error_type == "provider_access_denied"
    assert run.error_message == "subscription does not permit this feed"
    assert run.finished_at is not None
    assert await all_bars(sessions) == []


async def test_unexpected_error_records_only_its_class(sessions: Sessions) -> None:
    class ExplodingProvider(FakeProvider):
        async def get_bars(self, request: BarsRequest):  # type: ignore[override]
            raise ValueError("internal detail that should not be stored")

    with pytest.raises(ValueError):
        await BarIngestionService(ExplodingProvider(), sessions).ingest(REQUEST)

    (run,) = await all_runs(sessions)
    assert (run.status, run.error_type, run.error_message) == (
        "failed",
        "internal_error",
        "ValueError",
    )


async def test_truncation_is_recorded(sessions: Sessions) -> None:
    provider = FakeProvider(synthetic_daily_bars(2), truncated=True)
    result = await BarIngestionService(provider, sessions).ingest(REQUEST)

    assert result.truncated is True
    (run,) = await all_runs(sessions)
    assert run.truncated is True


async def test_run_records_requested_range_in_utc(sessions: Sessions) -> None:
    await BarIngestionService(FakeProvider(), sessions).ingest(REQUEST)
    (run,) = await all_runs(sessions)
    assert run.range_start == datetime(2024, 1, 1, tzinfo=UTC)
    assert run.range_end == datetime(2024, 1, 31, 23, 59, 59, tzinfo=UTC)
