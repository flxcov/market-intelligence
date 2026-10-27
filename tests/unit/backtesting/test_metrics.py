from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.marketpilot.backtesting.metrics import (
    ClosedTrade,
    EquityPoint,
    compute_metrics,
    daily_returns,
    max_drawdown,
)

D = Decimal
T0 = datetime(2024, 1, 2, 5, tzinfo=UTC)


def curve(*equities: str, step_days: int = 1) -> list[EquityPoint]:
    return [
        EquityPoint(T0 + timedelta(days=i * step_days), D(e), D(0)) for i, e in enumerate(equities)
    ]


def trade(pnl: str, *, entry="10", stop="9", qty=10, bars=5) -> ClosedTrade:
    return ClosedTrade(
        symbol="X",
        entry_bar=T0,
        exit_bar=T0,
        quantity=qty,
        entry_price=D(entry),
        exit_price=D(entry),
        initial_stop=D(stop),
        net_pnl=D(pnl),
        fees=D(0),
        bars_held=bars,
        exit_reason="test",
    )


def test_daily_returns_start_from_initial_equity() -> None:
    assert daily_returns(D(100), curve("110", "99")) == pytest.approx([0.10, -0.10])


def test_max_drawdown_depth_and_duration() -> None:
    # Peak 120 -> trough 90 is -25%; below the peak for 3 sessions before recovering.
    depth, sessions = max_drawdown(D(100), curve("120", "100", "90", "110", "125"))
    assert depth == pytest.approx(25.0)
    assert sessions == 3


def test_hand_checked_metrics() -> None:
    metrics = compute_metrics(
        start_equity=D(100),
        curve=curve("110", "99", "121"),
        trades=[trade("10"), trade("-5"), trade("20")],
        total_fees=D("1.5"),
        traded_notional=D(330),
    )
    assert metrics.total_return_pct == pytest.approx(21.0)
    assert metrics.max_drawdown_pct == pytest.approx(10.0)
    # returns 0.1, -0.1, 0.2222: mean 0.074074, sample stdev 0.162668 -> x sqrt(252)
    assert metrics.sharpe == pytest.approx(0.0740741 / 0.162668 * 252**0.5, rel=1e-4)
    assert metrics.win_rate_pct == pytest.approx(200 / 3)
    assert (metrics.average_win, metrics.average_loss) == (D(15), D(-5))
    assert metrics.profit_factor == pytest.approx(6.0)  # 30 / 5
    assert metrics.expectancy == D(25) / 3
    assert metrics.expectancy_r == pytest.approx((1.0 - 0.5 + 2.0) / 3)  # risk = 10 per trade
    assert metrics.turnover == pytest.approx(330 / ((110 + 99 + 121) / 3))
    assert metrics.cagr_pct is None  # 2 days of history: no annualization


def test_cagr_needs_a_year_and_compounds() -> None:
    two_years = curve("100", "200", step_days=730)
    metrics = compute_metrics(
        start_equity=D(100), curve=two_years, trades=[], total_fees=D(0), traded_notional=D(0)
    )
    assert metrics.cagr_pct == pytest.approx((2 ** (365.25 / 730) - 1) * 100)


def test_flat_curve_has_no_sharpe_and_no_trade_stats() -> None:
    metrics = compute_metrics(
        start_equity=D(100),
        curve=curve("100", "100", "100"),
        trades=[],
        total_fees=D(0),
        traded_notional=D(0),
    )
    assert (metrics.sharpe, metrics.sortino, metrics.volatility_annual_pct) == (None, None, 0.0)
    assert (metrics.win_rate_pct, metrics.profit_factor, metrics.expectancy) == (None, None, None)


def test_no_losing_trades_means_no_profit_factor() -> None:
    metrics = compute_metrics(
        start_equity=D(100),
        curve=curve("101", "102"),
        trades=[trade("1")],
        total_fees=D(0),
        traded_notional=D(10),
    )
    assert metrics.profit_factor is None


def test_risk_free_rate_lowers_sharpe() -> None:
    kwargs = {
        "start_equity": D(100),
        "curve": curve("101", "100.5", "102", "101.7"),
        "trades": [],
        "total_fees": D(0),
        "traded_notional": D(0),
    }
    assert compute_metrics(**kwargs, risk_free_rate_pct=5).sharpe < compute_metrics(**kwargs).sharpe
