"""Forward PAPER trading cycles against a real database, with a controllable clock.

All bars are SYNTHETIC. Bars are stored up front; each cycle only sees what was
complete at its `as_of` (bar timestamp + 1 day), exactly like running daily.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.backtesting.engine import BacktestEngine
from apps.marketpilot.execution.costs import CostModel
from apps.marketpilot.risk.limits import RiskLimits
from apps.marketpilot.services import signals as signals_module
from apps.marketpilot.services.forward_trading import (
    CreateDeploymentRequest,
    DeploymentConflictError,
    DeploymentRequestError,
    ForwardTradingService,
    RunCycleRequest,
    ShutdownRequest,
)
from apps.marketpilot.services.paper_models import CreateAccountRequest
from apps.marketpilot.services.paper_trading import PaperNotFoundError
from apps.marketpilot.strategies.trend_following import TrendFollowingStrategy
from packages.database.market_bars import SeriesKey, upsert_bars
from packages.database.models import PaperFillRecord, PaperOrderRecord, TradingCycleRecord
from packages.market_data.models import Adjustment, Bar, DataFeed
from tests.fakes import FakeProvider
from tests.strategy_data import backtest_scenario

pytestmark = pytest.mark.anyio

D = Decimal
Sessions = async_sessionmaker[AsyncSession]
BARS = backtest_scenario()


def cycle_time(index: int, bars: list[Bar] = BARS) -> datetime:
    """After bar `index` has completed, before the next session opens."""
    return bars[index].timestamp + timedelta(days=1, hours=1)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def store(sessions: Sessions, bars: list[Bar], symbol: str = "SYN") -> None:
    for adjustment in (Adjustment.RAW, Adjustment.SPLIT):
        async with sessions.begin() as session:
            await upsert_bars(
                session,
                SeriesKey(symbol, "1Day", DataFeed.IEX, adjustment),
                bars,
                provider="synthetic",
                retrieved_at=bars[-1].timestamp,
            )


async def setup(
    sessions: Sessions, clock: Clock, *, provider=None
) -> tuple[ForwardTradingService, int]:
    service = ForwardTradingService(
        sessions, default_feed=DataFeed.IEX, provider=provider, now=clock
    )
    account = await service._paper.create_account(
        CreateAccountRequest(name="forward", starting_cash=D(100_000))
    )
    deployment = await service.create(
        CreateDeploymentRequest(account_id=account.id, name="trend", symbols=["syn"])
    )
    return service, deployment.id


async def run_days(service, clock: Clock, deployment_id: int, first: int, last: int) -> list:
    cycles = []
    for index in range(first, last + 1):
        clock.now = cycle_time(index)
        cycles.append(
            await service.run_cycle(deployment_id, RunCycleRequest(as_of=clock.now, ingest=False))
        )
    return cycles


async def fills(sessions: Sessions) -> list[PaperFillRecord]:
    async with sessions() as session:
        return list((await session.scalars(select(PaperFillRecord).order_by("id"))).all())


async def count(sessions: Sessions, model) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model))


# --- the key property: forward trading reproduces the backtest ---------------------------------


async def test_daily_cycles_reproduce_the_backtest(sessions: Sessions) -> None:
    await store(sessions, BARS)
    clock = Clock(cycle_time(195))
    service, deployment_id = await setup(sessions, clock)

    cycles = await run_days(service, clock, deployment_id, 195, len(BARS) - 1)

    assert all(c.status == "succeeded" for c in cycles)
    (bt_trade,) = (
        BacktestEngine(
            strategy=TrendFollowingStrategy(),
            limits=RiskLimits(),
            costs=CostModel(),
            starting_cash=D(100_000),
        )
        .run({"SYN": BARS}, start=BARS[0].timestamp, end=BARS[-1].timestamp)
        .trades
    )
    buy, sell = await fills(sessions)
    assert (buy.side, buy.quantity, buy.price, buy.bar_timestamp) == (
        "buy",
        bt_trade.quantity,
        bt_trade.entry_price,
        bt_trade.entry_bar,
    )
    assert (sell.side, sell.price, sell.bar_timestamp) == (
        "sell",
        bt_trade.exit_price,
        bt_trade.exit_bar,
    )

    view = await service._paper.account_view(1)
    assert view.equity == D("100068.31")  # identical to the backtest's final equity
    assert (await service._paper.reconcile(1)).balanced

    exit_cycle = cycles[222 - 195]
    assert [e["exit_reason"] for e in exit_cycle.summary["exits"]] == ["trailing_exit"]
    entry_cycle = cycles[200 - 195]
    assert entry_cycle.summary["signals"] == {"SYN": "signal"}
    assert entry_cycle.summary["entries"][0]["accepted"] is True


# --- idempotency and restart recovery ------------------------------------------------------


async def test_rerunning_a_cycle_creates_nothing_new(sessions: Sessions) -> None:
    await store(sessions, BARS)
    clock = Clock(cycle_time(198))
    service, deployment_id = await setup(sessions, clock)
    # Through bar 222, whose close triggers the trailing exit: entry, stop and exit orders exist.
    await run_days(service, clock, deployment_id, 198, 222)
    orders_before, fills_before = (
        await count(sessions, PaperOrderRecord),
        len(await fills(sessions)),
    )
    assert orders_before == 3

    again = await service.run_cycle(deployment_id, RunCycleRequest(as_of=clock.now, ingest=False))
    clock.now += timedelta(hours=1)  # e.g. a scheduler retry an hour later
    later = await service.run_cycle(deployment_id, RunCycleRequest(as_of=clock.now, ingest=False))

    assert (again.status, later.status) == ("succeeded", "succeeded")
    assert await count(sessions, PaperOrderRecord) == orders_before
    assert len(await fills(sessions)) == fills_before


async def test_crash_mid_cycle_then_rerun_recovers_without_duplicates(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store(sessions, BARS)
    clock = Clock(cycle_time(198))
    service, deployment_id = await setup(sessions, clock)
    await run_days(service, clock, deployment_id, 198, 199)

    async def broken(self, request):
        raise RuntimeError("simulated crash during signal generation")

    with monkeypatch.context() as patch:
        patch.setattr(signals_module.SignalService, "generate", broken)
        clock.now = cycle_time(200)
        failed = await service.run_cycle(
            deployment_id, RunCycleRequest(as_of=clock.now, ingest=False)
        )
    assert (failed.status, failed.error_type, failed.error_message) == (
        "failed",
        "internal_error",
        "RuntimeError",
    )
    assert await count(sessions, PaperOrderRecord) == 0

    retried = await service.run_cycle(deployment_id, RunCycleRequest(as_of=clock.now, ingest=False))
    assert retried.status == "succeeded"
    assert retried.summary["entries"][0]["accepted"] is True

    await run_days(service, clock, deployment_id, 201, len(BARS) - 1)
    buy, sell = await fills(sessions)
    assert (buy.price, sell.price) == (D("141.90"), D("142.89"))  # same as uninterrupted


async def test_cycle_left_running_is_marked_interrupted(sessions: Sessions) -> None:
    await store(sessions, BARS)
    clock = Clock(cycle_time(198))
    service, deployment_id = await setup(sessions, clock)
    async with sessions.begin() as session:
        session.add(
            TradingCycleRecord(
                deployment_id=deployment_id,
                as_of=clock.now,
                status="running",
                started_at=clock.now,
                summary={},
            )
        )
    await service.run_cycle(deployment_id, RunCycleRequest(as_of=clock.now, ingest=False))

    history = await service.cycles(deployment_id, limit=5)
    assert [c.status for c in history] == ["succeeded", "interrupted"]


async def test_overlapping_cycles_are_refused(sessions: Sessions) -> None:
    clock = Clock(cycle_time(198))
    service, deployment_id = await setup(sessions, clock)
    async with sessions() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(72001, :id)"), {"id": deployment_id})
        with pytest.raises(DeploymentConflictError, match="already running"):
            await service.run_cycle(deployment_id, RunCycleRequest(as_of=clock.now, ingest=False))
        await holder.execute(text("SELECT pg_advisory_unlock(72001, :id)"), {"id": deployment_id})


# --- pause and emergency shutdown -----------------------------------------------------------


async def test_paused_deployment_manages_exits_but_opens_nothing(sessions: Sessions) -> None:
    await store(sessions, BARS)
    clock = Clock(cycle_time(198))
    service, deployment_id = await setup(sessions, clock)

    await service.set_status(deployment_id, "paused")
    paused = await run_days(service, clock, deployment_id, 198, 201)
    assert all("entries" not in c.summary for c in paused)
    assert await fills(sessions) == []  # the bar-200 breakout signal was not traded


async def test_pause_after_entry_still_exits(sessions: Sessions) -> None:
    await store(sessions, BARS)
    clock = Clock(cycle_time(198))
    service, deployment_id = await setup(sessions, clock)
    await run_days(service, clock, deployment_id, 198, 202)  # entered at bar 201
    await service.set_status(deployment_id, "paused")

    await run_days(service, clock, deployment_id, 203, len(BARS) - 1)

    _buy, sell = await fills(sessions)
    assert sell.price == D("142.89")


async def test_shutdown_halts_liquidates_and_is_final(sessions: Sessions) -> None:
    await store(sessions, BARS)
    clock = Clock(cycle_time(198))
    service, deployment_id = await setup(sessions, clock)
    await run_days(service, clock, deployment_id, 198, 202)

    result = await service.shutdown(deployment_id, ShutdownRequest(liquidate=True))
    assert result["deployment"]["status"] == "stopped"
    assert result["account_status"] == "halted"
    (liquidation,) = result["liquidation_orders"]
    assert (liquidation["symbol"], liquidation["accepted"], liquidation["quantity"]) == (
        "SYN",
        True,
        69,
    )

    (cycle,) = await run_days(service, clock, deployment_id, 203, 203)
    assert "exits" not in cycle.summary and "entries" not in cycle.summary  # fills only
    _buy, sell = await fills(sessions)
    assert (sell.side, sell.bar_timestamp) == ("sell", BARS[203].timestamp)
    assert (await service._paper.account_view(1)).positions[0].quantity == 0

    with pytest.raises(DeploymentConflictError, match="cannot be restarted"):
        await service.set_status(deployment_id, "active")


# --- ingestion step --------------------------------------------------------------------------


async def test_ingestion_through_the_provider(sessions: Sessions) -> None:
    clock = Clock(cycle_time(200))
    service, deployment_id = await setup(sessions, clock, provider=FakeProvider(BARS))

    cycle = await service.run_cycle(deployment_id, RunCycleRequest(as_of=clock.now))

    assert cycle.status == "succeeded"
    ingested = {(i["symbol"], i["adjustment"]): i for i in cycle.summary["ingestion"]}
    assert set(ingested) == {("SYN", "raw"), ("SYN", "split")}
    assert all(i["inserted"] > 200 for i in ingested.values())
    assert cycle.summary["signals"] == {"SYN": "signal"}  # signal from freshly ingested bars


async def test_missing_provider_makes_the_cycle_partial(sessions: Sessions) -> None:
    await store(sessions, BARS)
    clock = Clock(cycle_time(198))
    service, deployment_id = await setup(sessions, clock, provider=None)
    cycle = await service.run_cycle(deployment_id, RunCycleRequest(as_of=clock.now))
    assert cycle.status == "partial"
    assert "not configured" in cycle.summary["ingestion"][0]["error"]
    assert "processing" in cycle.summary  # the rest of the cycle still ran


# --- validation ------------------------------------------------------------------------------


async def test_errors(sessions: Sessions) -> None:
    clock = Clock(cycle_time(198))
    service, deployment_id = await setup(sessions, clock)
    with pytest.raises(PaperNotFoundError):
        await service.run_cycle(999, RunCycleRequest(ingest=False))
    with pytest.raises(DeploymentRequestError):
        await service.run_cycle(
            deployment_id, RunCycleRequest(as_of=clock.now + timedelta(days=1), ingest=False)
        )
    with pytest.raises(DeploymentConflictError):
        await service.create(CreateDeploymentRequest(account_id=1, name="trend", symbols=["X"]))
    with pytest.raises(PaperNotFoundError):
        await service.create(CreateDeploymentRequest(account_id=99, name="other", symbols=["X"]))
