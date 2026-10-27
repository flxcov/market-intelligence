"""Backtest engine on SYNTHETIC series with hand-derivable fills.

Base scenario (default parameters, 7 bps costs, volume 1,000,000/day):
  bars 0..199   close = 100 + 0.2i, spread 0.5   (steady uptrend, no breakout)
  bar 200       close 141.8 on 1.5x volume       (breakout -> signal at its close)
  bar 201       open 141.8 (= previous close)    -> entry 141.8 x 1.0007 = 141.90
                sized at worst entry 142.9071: position cap 10,000 / 143.01 = 69 shares
  bars 202..221 close rises 0.2/day to 146.0
  bar 222       close 143.0 < lowest low of previous 10 bars (143.7) -> exit decided
  bar 223       open 143.0 -> exit 143 x 0.9993 = 142.89 (rounded down)
  trade P&L = (142.89 - 141.90) x 69 = 68.31
"""

from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import pytest

from apps.marketpilot.backtesting.engine import BacktestEngine
from apps.marketpilot.execution.costs import CostModel
from apps.marketpilot.risk.limits import RiskLimits
from apps.marketpilot.strategies.trend_following import (
    TrendFollowingParams,
    TrendFollowingStrategy,
)
from tests.strategy_data import backtest_scenario

D = Decimal
VOLUME = D(1_000_000)


scenario = backtest_scenario


def run(bars, *, params=None, limits=None, start=None, end=None):
    engine = BacktestEngine(
        strategy=TrendFollowingStrategy(params),
        limits=limits or RiskLimits(),
        costs=CostModel(),
        starting_cash=D(100_000),
    )
    return engine.run(
        {"SYN": bars},
        start=start or bars[0].timestamp,
        end=end or bars[-1].timestamp,
    )


def cents_up(value: Decimal) -> Decimal:
    return value.quantize(D("0.01"), rounding=ROUND_CEILING)


def cents_down(value: Decimal) -> Decimal:
    return value.quantize(D("0.01"), rounding=ROUND_FLOOR)


def test_entry_and_trailing_exit_hand_checked() -> None:
    bars = scenario()
    result = run(bars)

    (trade,) = result.trades
    assert trade.entry_bar == bars[201].timestamp
    assert trade.entry_price == cents_up(D("141.8") * D("1.0007")) == D("141.90")
    assert trade.quantity == 69
    assert trade.initial_stop == D("139.5857")
    assert trade.exit_bar == bars[223].timestamp
    assert trade.exit_price == cents_down(D("143.0") * D("0.9993")) == D("142.89")
    assert trade.exit_reason == "trailing_exit"
    assert trade.net_pnl == D("68.31")
    assert trade.bars_held == 22
    assert result.curve[-1].equity == D("100068.31")
    assert result.open_trades == []
    assert result.traded_notional == D("141.90") * 69 + D("142.89") * 69


def test_protective_stop_gap_fills_at_the_open() -> None:
    # Bar 202 closes at 139.0: its open (previous close 142.0, clamped into the bar's
    # range) is 139.5, already below the 139.5857 stop -> fills at 139.5 x 0.9993.
    bars = scenario(after_entry=[D("139.0"), D("139.0")])
    (trade,) = run(bars).trades
    assert trade.exit_reason == "protective_stop"
    assert trade.exit_price == cents_down(D("139.5") * D("0.9993")) == D("139.40")
    assert trade.exit_bar == bars[202].timestamp


def test_entry_cancelled_when_open_gaps_above_the_bound() -> None:
    # Bar 201 closes at 145.0, so its open is clamped up to 144.5 > 142.9071: cancelled.
    # That close is itself a new breakout, so a fresh signal enters at bar 202's open.
    bars = scenario(entry_close=D("145.0"))
    result = run(bars)
    entries = [(e.timestamp, e.outcome, e.detail) for e in result.events if e.purpose == "entry"]
    assert entries[0] == (bars[201].timestamp, "cancelled", "open 144.5 above 142.9071")
    assert entries[1][:2] == (bars[202].timestamp, "filled")
    assert result.trades[0].entry_bar == bars[202].timestamp


def test_time_stop() -> None:
    params = TrendFollowingParams(max_holding_bars=5)
    (trade,) = run(scenario(), params=params).trades
    # Exit decided at the close 5 bars after entry, executed at the next open.
    assert (trade.exit_reason, trade.bars_held) == ("time_stop", 6)


def test_risk_gate_blocks_entries_and_counts_reasons() -> None:
    result = run(scenario(), limits=RiskLimits(min_price=D(1000)))
    assert result.trades == []
    assert result.rejections["min_price"] >= 1


def test_evaluations_are_counted_by_outcome() -> None:
    result = run(scenario())
    assert result.evaluations["insufficient_data"] == 199  # bars 0..198
    assert result.evaluations["signal"] == 1
    assert result.first_signal_evaluation == scenario()[199].timestamp


def test_warm_up_bars_feed_indicators_without_trading() -> None:
    bars = scenario()
    late_start = run(bars, start=bars[195].timestamp)
    assert late_start.curve[0].timestamp == bars[195].timestamp
    assert late_start.trades == run(bars).trades


# --- look-ahead protection ---------------------------------------------------------------


@pytest.mark.parametrize("cut", [150, 199, 200, 201, 202, 215, 222, 223])
def test_results_before_a_date_never_depend_on_later_data(cut: int) -> None:
    """Truncation invariance: dropping every bar from `cut` onward must leave all
    results for earlier sessions unchanged."""
    bars = scenario()
    full = run(bars)
    truncated = run(bars[:cut], end=bars[-1].timestamp)

    cutoff = bars[cut].timestamp
    assert [p for p in full.curve if p.timestamp < cutoff] == truncated.curve
    assert [t for t in full.trades if t.exit_bar < cutoff] == truncated.trades
    assert [e for e in full.events if e.timestamp < cutoff and e.detail != "backtest ended"] == [
        e for e in truncated.events if e.detail != "backtest ended"
    ]


def test_changing_future_prices_does_not_change_the_past() -> None:
    bars = scenario()
    crash = [b.model_copy(update={"low": D(1), "close": D(1), "open": D(1)}) for b in bars[215:]]
    altered = run([*bars[:215], *crash])
    cutoff = bars[215].timestamp
    assert [p for p in altered.curve if p.timestamp < cutoff] == [
        p for p in run(bars).curve if p.timestamp < cutoff
    ]


def test_runs_are_deterministic() -> None:
    bars = scenario()
    first, second = run(bars), run(bars)
    assert (first.curve, first.trades, first.events) == (second.curve, second.trades, second.events)


def test_open_position_at_end_is_reported_not_closed() -> None:
    bars = scenario()[:210]
    result = run(bars)
    assert result.trades == []
    (open_trade,) = result.open_trades
    assert (open_trade["symbol"], open_trade["quantity"]) == ("SYN", 69)
    assert result.curve[-1].equity == result.starting_cash + open_trade["unrealized_pnl"]


def test_multiple_symbols_share_one_account() -> None:
    bars = scenario()
    other = [b.model_copy(update={"timestamp": b.timestamp}) for b in bars]
    engine = BacktestEngine(
        strategy=TrendFollowingStrategy(),
        limits=RiskLimits(),
        costs=CostModel(),
        starting_cash=D(100_000),
    )
    result = engine.run(
        {"AAA": bars, "BBB": other}, start=bars[0].timestamp, end=bars[-1].timestamp
    )
    assert sorted(t.symbol for t in result.trades) == ["AAA", "BBB"]
    # Both entries filled at the same open: the second saw the first's cash reserved.
    assert {t.quantity for t in result.trades} == {69}


def test_bars_after_end_are_ignored() -> None:
    bars = scenario()
    result = run(bars, end=bars[210].timestamp)
    assert result.curve[-1].timestamp == bars[210].timestamp
    assert result.curve[-1].timestamp + timedelta(days=1) == bars[211].timestamp


def test_market_exit_fills_at_the_open_before_an_intraday_stop() -> None:
    """Exit decided at bar 222's close; bar 223 opens at 143 (above the 139.5857 stop)
    and then trades down to 139. The market exit executes at the open first; the
    protective stop must not fill at the (worse) stop price."""
    bars = scenario()
    bars[223] = bars[223].model_copy(
        update={"open": D(143), "high": D("143.2"), "low": D(139), "close": D("139.3")}
    )
    (trade,) = run(bars).trades
    assert (trade.exit_reason, trade.exit_price) == ("trailing_exit", D("142.89"))


def test_orders_are_never_eligible_on_the_bar_that_created_them() -> None:
    from apps.marketpilot.backtesting.engine import _Order
    from apps.marketpilot.execution.models import OrderSide, OrderSpec, OrderType

    bar_time = scenario()[200].timestamp
    order = _Order(
        1,
        "SYN",
        OrderSpec(side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=1),
        created_at=bar_time,
        purpose="entry",
        day_only=True,
    )
    engine = BacktestEngine(
        strategy=TrendFollowingStrategy(),
        limits=RiskLimits(),
        costs=CostModel(),
        starting_cash=D(1),
    )
    assert engine._eligible(order, bar_time) is False
    assert engine._eligible(order, bar_time + timedelta(days=1)) is True


# --- seeded random-walk properties ------------------------------------------------------


def random_walk(seed: int, n: int = 750) -> list:
    import random
    from datetime import UTC, datetime

    from packages.market_data.models import Bar

    rng = random.Random(seed)
    price = 50.0
    bars = []
    for i in range(n):
        o = price
        c = max(1.0, o * (1 + rng.gauss(0.0004, 0.02)))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.006)))
        low = min(o, c) * (1 - abs(rng.gauss(0, 0.006)))
        bars.append(
            Bar(
                timestamp=datetime(2018, 1, 1, 5, tzinfo=UTC) + timedelta(days=i),
                open=D(f"{o:.2f}"),
                high=D(f"{h:.2f}"),
                low=D(f"{low:.2f}"),
                close=D(f"{c:.2f}"),
                volume=D(rng.randint(500_000, 3_000_000)),
            )
        )
        price = c
    return bars


def test_random_walk_invariants() -> None:
    """Seeded synthetic data (not market data): accounting and sanity invariants."""
    data = {f"R{i}": random_walk(i) for i in range(3)}
    timestamps = data["R0"]
    engine = BacktestEngine(
        strategy=TrendFollowingStrategy(),
        limits=RiskLimits(),
        costs=CostModel(commission_per_share=D("0.005"), min_commission=D(1)),
        starting_cash=D(100_000),
    )
    result = engine.run(data, start=timestamps[0].timestamp, end=timestamps[-1].timestamp)

    assert len(result.trades) >= 10  # the scenario actually exercises trading
    assert engine.portfolio.valuation(engine.marks).identity_difference == 0
    assert all(p.equity - p.market_value >= 0 for p in result.curve)  # cash never negative
    for trade in result.trades:
        assert trade.entry_bar < trade.exit_bar or trade.exit_reason == "protective_stop"
        assert trade.initial_stop < trade.entry_price
        assert trade.fees >= D(2)  # $1 minimum commission each side
    # Exact balance: everything earned or paid is accounted for, to the cent.
    closed_pnl = sum((t.net_pnl for t in result.trades), D(0))
    open_pnl = sum((t["unrealized_pnl"] for t in result.open_trades), D(0))
    open_entry_fees = sum((t.entry_fee for t in engine.open_trades.values()), D(0))
    assert result.curve[-1].equity == (
        result.starting_cash + closed_pnl + open_pnl - open_entry_fees
    )


@pytest.mark.parametrize("cut", [300, 451, 600])
def test_random_walk_truncation_invariance(cut: int) -> None:
    data = {f"R{i}": random_walk(i) for i in range(2)}
    start, end = data["R0"][0].timestamp, data["R0"][-1].timestamp

    def run_on(series):
        engine = BacktestEngine(
            strategy=TrendFollowingStrategy(),
            limits=RiskLimits(),
            costs=CostModel(),
            starting_cash=D(100_000),
        )
        return engine.run(series, start=start, end=end)

    full = run_on(data)
    short = run_on({s: b[:cut] for s, b in data.items()})
    cutoff = data["R0"][cut].timestamp
    assert [p for p in full.curve if p.timestamp < cutoff] == short.curve
    assert [t for t in full.trades if t.exit_bar < cutoff] == short.trades
