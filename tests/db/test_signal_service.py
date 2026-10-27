from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.services.signals import SignalGenerationRequest, SignalService
from apps.marketpilot.strategies.base import Outcome
from packages.database.market_bars import SeriesKey, upsert_bars
from packages.database.models import SignalRecord, StrategyConfigRecord
from packages.market_data.models import Adjustment, Bar, DataFeed
from tests.fakes import RETRIEVED_AT
from tests.strategy_data import as_of_after, breakout_series, linear_closes, make_bars

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]
SPLIT = Adjustment.SPLIT


async def store(sessions: Sessions, symbol: str, bars: list[Bar], adjustment=SPLIT) -> None:
    async with sessions.begin() as session:
        await upsert_bars(
            session,
            SeriesKey(symbol, "1Day", DataFeed.IEX, adjustment),
            bars,
            provider="fake",
            retrieved_at=RETRIEVED_AT,
        )


async def count(sessions: Sessions, model) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model))


def service(sessions: Sessions) -> SignalService:
    return SignalService(sessions, default_feed=DataFeed.IEX)


def request(symbols: list[str], as_of, **kwargs) -> SignalGenerationRequest:
    return SignalGenerationRequest(symbols=symbols, as_of=as_of, **kwargs)


async def test_signal_is_generated_from_stored_bars_and_recorded(sessions: Sessions) -> None:
    bars = breakout_series()
    await store(sessions, "UP", bars)

    result = await service(sessions).generate(request(["UP"], as_of_after(bars)))

    (item,) = result.results
    assert item.evaluation.outcome is Outcome.SIGNAL
    assert item.signal_id is not None and item.already_existed is False
    assert item.data_retrieved_at == RETRIEVED_AT
    async with sessions() as session:
        record = await session.get_one(SignalRecord, item.signal_id)
    assert record.signal_bar_at == bars[-1].timestamp
    assert record.reference_price == Decimal("141.8")
    assert record.stop_price == Decimal("139.5857")
    assert record.status == "new" and record.side == "long"
    assert record.score_components["relative_volume"] == "1.5000"
    assert [c["rule"] for c in record.rule_checks] == [
        "trend_alignment",
        "above_slow_ma",
        "breakout",
        "valid_stop",
    ]


async def test_regenerating_is_idempotent(sessions: Sessions) -> None:
    bars = breakout_series()
    await store(sessions, "UP", bars)
    as_of = as_of_after(bars)

    first = (await service(sessions).generate(request(["UP"], as_of))).results[0]
    later = as_of + timedelta(hours=2)  # e.g. a restart re-runs generation
    second = (await service(sessions).generate(request(["UP"], later))).results[0]

    assert second.signal_id == first.signal_id
    assert second.already_existed is True
    assert await count(sessions, SignalRecord) == 1
    assert await count(sessions, StrategyConfigRecord) == 1


async def test_only_signal_outcomes_are_stored(sessions: Sessions) -> None:
    breakout = breakout_series()
    await store(sessions, "UP", breakout)
    await store(sessions, "FLAT", make_bars(linear_closes(201)))  # no breakout
    await store(sessions, "SHORT", make_bars(linear_closes(50)))  # too little history

    result = await service(sessions).generate(
        request(["UP", "FLAT", "SHORT", "NONE"], as_of_after(breakout))
    )

    outcomes = {r.evaluation.symbol: r.evaluation.outcome for r in result.results}
    assert outcomes == {
        "UP": Outcome.SIGNAL,
        "FLAT": Outcome.NO_SIGNAL,
        "SHORT": Outcome.INSUFFICIENT_DATA,
        "NONE": Outcome.INSUFFICIENT_DATA,
    }
    assert await count(sessions, SignalRecord) == 1
    none_result = result.results[3]
    assert any("Ingest them first" in n for n in none_result.notices)


async def test_raw_bars_are_never_used(sessions: Sessions) -> None:
    bars = breakout_series()
    await store(sessions, "UP", bars, adjustment=Adjustment.RAW)

    result = await service(sessions).generate(request(["UP"], as_of_after(bars)))

    assert result.results[0].evaluation.outcome is Outcome.INSUFFICIENT_DATA


async def test_replay_in_the_past_ignores_later_bars(sessions: Sessions) -> None:
    bars = breakout_series()
    await store(sessions, "UP", bars)

    # Before the breakout bar finished, the same data gives no signal.
    result = await service(sessions).generate(request(["UP"], as_of_after(bars[:-1])))

    assert result.results[0].evaluation.outcome is Outcome.NO_SIGNAL
    assert result.results[0].evaluation.last_completed_bar == bars[-2].timestamp


async def test_different_parameters_create_separate_config_and_signal(sessions: Sessions) -> None:
    bars = breakout_series()
    await store(sessions, "UP", bars)
    as_of = as_of_after(bars)

    default = await service(sessions).generate(request(["UP"], as_of))
    wider = await service(sessions).generate(
        request(["UP"], as_of, parameters={"stop_atr_multiple": "3"})
    )

    assert default.parameters_hash != wider.parameters_hash
    assert default.results[0].signal_id != wider.results[0].signal_id
    assert await count(sessions, StrategyConfigRecord) == 2


async def test_revised_bars_do_not_rewrite_a_recorded_signal(sessions: Sessions) -> None:
    bars = breakout_series()
    await store(sessions, "UP", bars)
    as_of = as_of_after(bars)
    first = (await service(sessions).generate(request(["UP"], as_of))).results[0]

    revised = bars[-1].model_copy(update={"close": Decimal("142.0")})
    await store(sessions, "UP", [*bars[:-1], revised])
    second = (await service(sessions).generate(request(["UP"], as_of))).results[0]

    assert second.signal_id == first.signal_id
    assert any("original signal is kept" in n for n in second.notices)
    async with sessions() as session:
        record = await session.get_one(SignalRecord, first.signal_id)
    assert record.reference_price == Decimal("141.8")
