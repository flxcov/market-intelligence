from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.services.backtests import (
    BacktestNotFoundError,
    BacktestRequest,
    BacktestRequestError,
    BacktestService,
)
from apps.marketpilot.services.paper_models import CreateAccountRequest, SignalOrderRequest
from apps.marketpilot.services.paper_trading import PaperTradingService
from apps.marketpilot.services.signals import SignalGenerationRequest, SignalService
from packages.database.market_bars import SeriesKey, upsert_bars
from packages.market_data.models import Adjustment, Bar, DataFeed
from tests.strategy_data import as_of_after, backtest_scenario, linear_closes, make_bars

pytestmark = pytest.mark.anyio

D = Decimal
Sessions = async_sessionmaker[AsyncSession]
BARS = backtest_scenario()


async def store(sessions: Sessions, symbol: str, bars: list[Bar], adjustment=Adjustment.ALL):
    async with sessions.begin() as session:
        await upsert_bars(
            session,
            SeriesKey(symbol, "1Day", DataFeed.IEX, adjustment),
            bars,
            provider="synthetic",
            retrieved_at=bars[-1].timestamp + timedelta(days=2),
        )


def request(**overrides) -> BacktestRequest:
    fields = {
        "symbols": ["SYN"],
        "start": BARS[0].timestamp,
        "end": BARS[-1].timestamp,
        "benchmark": "BENCH",
    }
    return BacktestRequest(**{**fields, **overrides})


def service(sessions: Sessions) -> BacktestService:
    return BacktestService(sessions, default_feed=DataFeed.IEX)


async def test_backtest_reproduces_the_hand_checked_trade_and_is_stored(sessions: Sessions) -> None:
    await store(sessions, "SYN", BARS)
    # Benchmark: 100 -> 144.6 over the same sessions (linear +0.2/day).
    await store(sessions, "BENCH", make_bars(linear_closes(len(BARS))))

    report = await service(sessions).run(request())

    assert report.mode == "BACKTEST"
    (trade,) = report.trades
    assert (trade.quantity, trade.entry_price, trade.exit_price, trade.net_pnl) == (
        69,
        D("141.90"),
        D("142.89"),
        D("68.31"),
    )
    assert report.metrics.end_equity == D("100068.31")
    assert report.benchmark is not None
    assert report.benchmark.total_return_pct == pytest.approx((144.6 / 100 - 1) * 100)
    assert report.benchmark.strategy_excess_return_pct == pytest.approx(
        report.metrics.total_return_pct - report.benchmark.total_return_pct
    )
    assert any("Survivorship bias" in w for w in report.warnings)
    assert any("Only 1 closed trades" in w for w in report.warnings)
    assert report.id is not None and report.created_at is not None

    stored = await service(sessions).get(report.id)
    assert stored.model_dump(exclude={"created_at"}) == report.model_dump(exclude={"created_at"})
    (summary,) = await service(sessions).list(limit=10)
    assert (summary.id, summary.trades, summary.symbols) == (report.id, 1, ["SYN"])


async def test_same_data_same_request_same_result(sessions: Sessions) -> None:
    await store(sessions, "SYN", BARS)
    first = await service(sessions).run(request(benchmark=None))
    second = await service(sessions).run(request(benchmark=None))
    assert first.data_fingerprint == second.data_fingerprint
    assert first.metrics == second.metrics
    assert first.trades == second.trades


async def test_revised_data_changes_the_fingerprint(sessions: Sessions) -> None:
    await store(sessions, "SYN", BARS)
    before = await service(sessions).run(request(benchmark=None))
    await store(sessions, "SYN", [BARS[5].model_copy(update={"close": BARS[5].close + D("0.01")})])
    after = await service(sessions).run(request(benchmark=None))
    assert before.data_fingerprint != after.data_fingerprint


async def test_out_of_sample_split(sessions: Sessions) -> None:
    await store(sessions, "SYN", BARS)
    report = await service(sessions).run(
        request(benchmark=None, out_of_sample_start=BARS[210].timestamp)
    )
    assert report.in_sample is not None and report.out_of_sample is not None
    assert report.in_sample.sessions + report.out_of_sample.sessions == report.metrics.sessions
    assert report.in_sample.trades == 1  # entered before the split
    assert report.out_of_sample.trades == 0
    assert report.out_of_sample.start_equity == report.equity_curve[209].equity
    assert any("Out-of-sample split" in w for w in report.warnings)


async def test_missing_data_is_reported(sessions: Sessions) -> None:
    await store(sessions, "SYN", BARS)
    report = await service(sessions).run(request(symbols=["SYN", "NODATA"]))
    assert report.symbols_without_data == ["NODATA"]
    assert any("Benchmark BENCH has no stored" in w for w in report.warnings)

    with pytest.raises(BacktestRequestError, match="ingest them first"):
        await service(sessions).run(request(symbols=["NODATA"]))


async def test_only_adjusted_bars_are_used(sessions: Sessions) -> None:
    await store(sessions, "SYN", BARS, adjustment=Adjustment.RAW)
    with pytest.raises(BacktestRequestError):
        await service(sessions).run(request(benchmark=None))
    with pytest.raises(ValidationError, match="adjusted prices"):
        request(adjustment="raw")


async def test_unknown_backtest_is_404(sessions: Sessions) -> None:
    with pytest.raises(BacktestNotFoundError):
        await service(sessions).get(12345)


@pytest.mark.parametrize(
    "overrides",
    [
        {"end": "2999-01-01"},
        {"start": "2024-02-01", "end": "2024-01-01"},
        {"start": "2000-01-01", "end": "2024-01-01"},  # longer than 15 years
        {"out_of_sample_start": "1999-01-01"},
        {"symbols": [f"S{i}" for i in range(21)]},
    ],
)
def test_invalid_requests(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        request(**overrides)


# --- parity: the backtest and PAPER trading must agree --------------------------------------


async def test_backtest_and_paper_trading_produce_identical_fills(sessions: Sessions) -> None:
    """Same bars, same signal: the backtest's entry and stop-out must match PAPER exactly."""
    bars = backtest_scenario(after_entry=[D("139.0"), D("139.0")])  # stop gap on bar 202
    signal_part = bars[:201]  # up to and including the signal bar (200)

    # Backtest (adjusted series).
    await store(sessions, "UP", bars)
    report = await service(sessions).run(
        request(symbols=["UP"], start=bars[0].timestamp, end=bars[-1].timestamp, benchmark=None)
    )
    (bt,) = report.trades

    # PAPER: signal from split bars, fills on raw bars (identical values: no splits here).
    await store(sessions, "UP", signal_part, adjustment=Adjustment.SPLIT)
    await store(sessions, "UP", signal_part, adjustment=Adjustment.RAW)
    signal_time = as_of_after(signal_part)
    generated = await SignalService(sessions, default_feed=DataFeed.IEX).generate(
        SignalGenerationRequest(symbols=["UP"], as_of=signal_time)
    )
    signal_id = generated.results[0].signal_id
    assert signal_id is not None

    def paper(now):
        return PaperTradingService(sessions, default_feed=DataFeed.IEX, now=lambda: now)

    account = await paper(signal_time).create_account(
        CreateAccountRequest(name="parity", starting_cash=D(100_000))
    )
    await paper(signal_time).submit_signal_order(
        account.id, SignalOrderRequest(signal_id=signal_id)
    )
    await store(sessions, "UP", bars[201:], adjustment=Adjustment.RAW)
    await paper(as_of_after(bars)).process(account.id)

    sell, buy = await paper(as_of_after(bars)).list_fills(account.id, limit=5)
    assert (buy.quantity, buy.price, buy.bar_timestamp) == (
        bt.quantity,
        bt.entry_price,
        bt.entry_bar,
    )
    assert (sell.price, sell.bar_timestamp) == (bt.exit_price, bt.exit_bar)
    assert bt.exit_reason == "protective_stop"
    view = await paper(as_of_after(bars)).account_view(account.id)
    assert view.equity == report.metrics.end_equity
