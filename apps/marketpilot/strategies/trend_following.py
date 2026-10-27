"""Trend following v1: Donchian-style breakout in an established uptrend (long only).

Evaluated on the most recent COMPLETED daily bar `t`. All four rules must pass:

  1. trend_alignment   SMA(fast) > SMA(slow)
  2. above_slow_ma     close[t] > SMA(slow)
  3. breakout          close[t] > highest high of the `breakout_lookback` bars BEFORE t
  4. valid_stop        stop = close[t] - stop_atr_multiple * ATR(atr_period) is > 0

Signal plan (nothing executes at close[t]; that price is already in the past):
  entry   market order at the NEXT session's open; valid for that session only.
          Cancel if that open is below the stop (already invalidated) or more than
          `max_entry_gap_atr` ATRs above close[t] (risk would exceed the plan).
  exits   whichever comes first:
          - initial stop is hit;
          - a close below the lowest low of the previous `exit_lookback` bars;
          - `max_holding_bars` bars after entry (time stop).

There is deliberately no profit target and therefore no reward:risk ratio: a
trend-following edge, if any, comes from letting winners run. Whether it has
an edge at all must be established by backtesting (a later phase), not assumed.

Use split-adjusted (or fully adjusted) bars: on raw prices a stock split looks
like a crash and corrupts every moving average.
"""

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.marketpilot.strategies.base import (
    RATIO_QUANTUM,
    EntryPlan,
    ExitPlan,
    RuleCheck,
    SignalProposal,
    Strategy,
    check,
    quantize,
)
from apps.marketpilot.strategies.indicators import (
    average_true_range,
    highest_high,
    lowest_low,
    mean,
    simple_moving_average,
)
from packages.market_data.models import Bar


class TrendFollowingParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fast_ma_period: int = Field(default=50, ge=2, le=400)
    slow_ma_period: int = Field(default=200, ge=3, le=400)
    breakout_lookback: int = Field(default=20, ge=2, le=250)
    atr_period: int = Field(default=14, ge=2, le=100)
    stop_atr_multiple: Decimal = Field(default=Decimal("2"), gt=0, le=10)
    max_entry_gap_atr: Decimal = Field(default=Decimal("1"), gt=0, le=5)
    exit_lookback: int = Field(default=10, ge=2, le=250)
    max_holding_bars: int = Field(default=60, ge=1, le=1000)
    volume_lookback: int = Field(default=20, ge=2, le=250)
    # Data-quality limits. There is no market calendar yet, so these are calendar
    # days chosen to tolerate weekends plus a holiday.
    max_bar_age_days: int = Field(default=4, ge=1, le=30)
    max_gap_days: int = Field(default=6, ge=1, le=30)

    @field_validator("stop_atr_multiple", "max_entry_gap_atr")
    @classmethod
    def _canonical_decimal(cls, value: Decimal) -> Decimal:
        # "2", "2.0" and "2.00" must hash identically.
        return Decimal(format(value.normalize(), "f"))

    @model_validator(mode="after")
    def _fast_below_slow(self) -> Self:
        if self.fast_ma_period >= self.slow_ma_period:
            raise ValueError("fast_ma_period must be smaller than slow_ma_period")
        return self


class TrendFollowingStrategy(Strategy):
    name = "trend_following"
    version = "1.0.0"
    supported_timeframes = frozenset({"1Day"})
    params: TrendFollowingParams

    def __init__(self, params: TrendFollowingParams | None = None) -> None:
        super().__init__(params or TrendFollowingParams())

    @property
    def required_bars(self) -> int:
        p = self.params
        # Windows that exclude the signal bar need one extra bar.
        return max(
            p.slow_ma_period,
            p.breakout_lookback + 1,
            p.atr_period + 1,
            p.volume_lookback + 1,
        )

    @property
    def max_bar_age(self) -> timedelta:
        return timedelta(days=self.params.max_bar_age_days)

    @property
    def max_gap(self) -> timedelta:
        return timedelta(days=self.params.max_gap_days)

    def _evaluate_rules(self, bars: Sequence[Bar]) -> tuple[list[RuleCheck], SignalProposal | None]:
        p = self.params
        bar = bars[-1]
        before = bars[:-1]  # strictly earlier bars
        closes = [b.close for b in bars]

        fast = simple_moving_average(closes, p.fast_ma_period)
        slow = simple_moving_average(closes, p.slow_ma_period)
        prior_high = highest_high(before[-p.breakout_lookback :])
        atr = average_true_range(bars, p.atr_period)
        stop = bar.close - p.stop_atr_multiple * atr

        checks = [
            check(
                "trend_alignment",
                fast > slow,
                f"SMA{p.fast_ma_period}={quantize(fast)} vs SMA{p.slow_ma_period}={quantize(slow)}",
            ),
            check(
                "above_slow_ma",
                bar.close > slow,
                f"close={bar.close} vs SMA{p.slow_ma_period}={quantize(slow)}",
            ),
            check(
                "breakout",
                bar.close > prior_high,
                f"close={bar.close} vs prior {p.breakout_lookback}-bar high={prior_high}",
            ),
            check(
                "valid_stop",
                atr > 0 and stop > 0,
                f"stop={quantize(stop)} (close - {p.stop_atr_multiple} x ATR{p.atr_period}"
                f"={quantize(atr)})",
            ),
        ]
        if not all(c.passed for c in checks):
            return checks, None

        avg_volume = mean([b.volume for b in before[-p.volume_lookback :]])
        risk = bar.close - stop
        proposal = SignalProposal(
            entry=EntryPlan(
                reference_price=bar.close,
                cancel_if_open_below=quantize(stop),
                cancel_if_open_above=quantize(bar.close + p.max_entry_gap_atr * atr),
                expires_after_bars=1,
            ),
            exit=ExitPlan(
                initial_stop=quantize(stop),
                trailing_exit_lookback_bars=p.exit_lookback,
                time_stop_bars=p.max_holding_bars,
                rules=[
                    f"Exit if price trades at or below the initial stop {quantize(stop)}.",
                    f"Exit at the next open after a close below the lowest low of the "
                    f"previous {p.exit_lookback} bars.",
                    f"Exit at the next open after {p.max_holding_bars} bars in the trade.",
                ],
            ),
            atr=quantize(atr),
            risk_per_share=quantize(risk),
            risk_pct_of_price=quantize(risk / bar.close * 100, RATIO_QUANTUM),
            score_components={
                "trend_spread_pct": quantize((fast - slow) / slow * 100, RATIO_QUANTUM),
                "breakout_size_atr": quantize((bar.close - prior_high) / atr, RATIO_QUANTUM),
                "relative_volume": (
                    quantize(bar.volume / avg_volume, RATIO_QUANTUM) if avg_volume > 0 else None
                ),
            },
        )
        return checks, proposal

    def exit_reason(self, history: Sequence[Bar], bars_held: int) -> str | None:
        """Strategy exit rule evaluated at the close of history[-1] (completed bars only).

        `bars_held` counts bars since the entry bar (the entry bar itself is 0).
        Returns "trailing_exit", "time_stop" or None. The protective stop is a
        separate resting order, not part of this check. Used by both the
        backtester and forward PAPER trading so the two cannot diverge.
        """
        p = self.params
        previous = history[-(p.exit_lookback + 1) : -1]
        if len(previous) == p.exit_lookback and history[-1].close < lowest_low(previous):
            return "trailing_exit"
        if bars_held >= p.max_holding_bars:
            return "time_stop"
        return None
