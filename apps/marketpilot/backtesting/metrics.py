"""Performance metrics for an equity curve and a list of closed trades.

Precision strategy: money (equity, P&L, fees) stays Decimal. Statistical
estimates (volatility, Sharpe, Sortino, CAGR) use float: they are estimates
with sampling error far larger than float rounding, and need sqrt/pow.

Conventions (stated so results can be interpreted):
  - daily returns are close-to-close equity changes between processed sessions;
  - annualization uses 252 sessions per year (sqrt(252) for volatility);
  - Sharpe = mean(daily excess return) / sample stdev x sqrt(252);
  - Sortino uses downside deviation of excess returns over all sessions;
  - CAGR is only reported for periods of at least 365 days;
  - max drawdown is peak-to-trough on session closes (intraday lows unseen).
"""

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

SESSIONS_PER_YEAR = 252
MIN_DAYS_FOR_ANNUALIZATION = 365


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    equity: Decimal
    market_value: Decimal


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    entry_bar: datetime
    exit_bar: datetime
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    initial_stop: Decimal
    net_pnl: Decimal  # after both commissions
    fees: Decimal
    bars_held: int
    exit_reason: str

    @property
    def initial_risk(self) -> Decimal:
        return (self.entry_price - self.initial_stop) * self.quantity

    @property
    def r_multiple(self) -> Decimal | None:
        risk = self.initial_risk
        return self.net_pnl / risk if risk > 0 else None


class PerformanceMetrics(BaseModel):
    sessions: int
    start_equity: Decimal
    end_equity: Decimal
    total_return_pct: float
    cagr_pct: float | None
    max_drawdown_pct: float
    max_drawdown_sessions: int
    volatility_annual_pct: float | None
    sharpe: float | None
    sortino: float | None
    exposure_pct: float
    average_gross_exposure_pct: float
    trades: int
    win_rate_pct: float | None
    average_win: Decimal | None
    average_loss: Decimal | None
    profit_factor: float | None
    expectancy: Decimal | None
    expectancy_r: float | None
    average_bars_held: float | None
    total_fees: Decimal
    traded_notional: Decimal
    turnover: float | None


def daily_returns(start_equity: Decimal, curve: Sequence[EquityPoint]) -> list[float]:
    previous = start_equity
    returns = []
    for point in curve:
        returns.append(float(point.equity / previous - 1) if previous else 0.0)
        previous = point.equity
    return returns


def max_drawdown(start_equity: Decimal, curve: Sequence[EquityPoint]) -> tuple[float, int]:
    """(largest peak-to-trough decline in %, longest stretch below a prior peak in sessions)."""
    peak = start_equity
    worst = 0.0
    longest = current = 0
    for point in curve:
        if point.equity >= peak:
            peak = point.equity
            current = 0
        else:
            current += 1
            longest = max(longest, current)
            worst = max(worst, float((peak - point.equity) / peak * 100))
    return worst, longest


def compute_metrics(
    *,
    start_equity: Decimal,
    curve: Sequence[EquityPoint],
    trades: Sequence[ClosedTrade],
    total_fees: Decimal,
    traded_notional: Decimal,
    risk_free_rate_pct: float = 0.0,
) -> PerformanceMetrics:
    end_equity = curve[-1].equity if curve else start_equity
    returns = daily_returns(start_equity, curve)
    rf_daily = risk_free_rate_pct / 100 / SESSIONS_PER_YEAR
    excess = [r - rf_daily for r in returns]

    volatility = sharpe = sortino = None
    if len(returns) >= 2:
        stdev = statistics.stdev(returns)
        volatility = stdev * math.sqrt(SESSIONS_PER_YEAR) * 100
        if stdev > 0:
            sharpe = (
                statistics.fmean(excess) / statistics.stdev(excess) * math.sqrt(SESSIONS_PER_YEAR)
            )
        downside = math.sqrt(statistics.fmean([min(0.0, r) ** 2 for r in excess]))
        if downside > 0:
            sortino = statistics.fmean(excess) / downside * math.sqrt(SESSIONS_PER_YEAR)

    cagr = None
    if curve:
        days = (curve[-1].timestamp - curve[0].timestamp).days
        if days >= MIN_DAYS_FOR_ANNUALIZATION and end_equity > 0:
            cagr = ((float(end_equity / start_equity)) ** (365.25 / days) - 1) * 100

    drawdown, drawdown_sessions = max_drawdown(start_equity, curve)
    exposures = [float(p.market_value / p.equity) for p in curve if p.equity > 0]
    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl <= 0]
    gross_loss = -sum(losses, Decimal(0))
    r_values = [float(r) for t in trades if (r := t.r_multiple) is not None]
    average_equity = (
        sum((p.equity for p in curve), Decimal(0)) / len(curve) if curve else start_equity
    )

    return PerformanceMetrics(
        sessions=len(curve),
        start_equity=start_equity,
        end_equity=end_equity,
        total_return_pct=float((end_equity / start_equity - 1) * 100),
        cagr_pct=cagr,
        max_drawdown_pct=drawdown,
        max_drawdown_sessions=drawdown_sessions,
        volatility_annual_pct=volatility,
        sharpe=sharpe,
        sortino=sortino,
        exposure_pct=(
            sum(1 for p in curve if p.market_value > 0) / len(curve) * 100 if curve else 0.0
        ),
        average_gross_exposure_pct=statistics.fmean(exposures) * 100 if exposures else 0.0,
        trades=len(trades),
        win_rate_pct=len(wins) / len(trades) * 100 if trades else None,
        average_win=sum(wins, Decimal(0)) / len(wins) if wins else None,
        average_loss=sum(losses, Decimal(0)) / len(losses) if losses else None,
        profit_factor=(
            float(sum(wins, Decimal(0)) / gross_loss) if gross_loss > 0 and trades else None
        ),
        expectancy=sum((t.net_pnl for t in trades), Decimal(0)) / len(trades) if trades else None,
        expectancy_r=statistics.fmean(r_values) if r_values else None,
        average_bars_held=statistics.fmean([t.bars_held for t in trades]) if trades else None,
        total_fees=total_fees,
        traded_notional=traded_notional,
        turnover=float(traded_notional / average_equity) if average_equity else None,
    )
