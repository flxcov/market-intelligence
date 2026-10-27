"""SYNTHETIC daily price series with hand-checkable properties (not market data).

Bars are spaced one calendar day apart at 05:00Z. Each bar has
high = close + spread, low = close - spread, open = previous close clamped
into [low, high], so every bar is internally consistent.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from packages.market_data.models import Bar

FIRST_BAR = datetime(2023, 1, 1, 5, tzinfo=UTC)
SPREAD = Decimal("0.5")


def make_bars(
    closes: Sequence[Decimal],
    *,
    volumes: Sequence[Decimal] | None = None,
    first: datetime = FIRST_BAR,
    spread: Decimal = SPREAD,
) -> list[Bar]:
    bars = []
    for i, close in enumerate(closes):
        high, low = close + spread, close - spread
        previous = closes[i - 1] if i else close
        bars.append(
            Bar(
                timestamp=first + timedelta(days=i),
                open=min(max(previous, low), high),
                high=high,
                low=low,
                close=close,
                volume=volumes[i] if volumes else Decimal(1000),
            )
        )
    return bars


def linear_closes(count: int, *, start: Decimal = Decimal(100), step: Decimal = Decimal("0.2")):
    return [start + step * i for i in range(count)]


def breakout_series() -> list[Bar]:
    """200 bars rising 0.2/day from 100, then one bar closing 2.0 higher on 1.5x volume.

    Hand-derived values for the final bar with default parameters
    (window = the last 200 bars = indices 1..200):
      close = 141.8; prior 20-bar high = 139.8 + 0.5 = 140.3
      SMA200 = (sum(100 + 0.2i, i=1..199) + 141.8) / 200 = 24021.8 / 200 = 120.109
      SMA50  = (sum(100 + 0.2i, i=151..199) + 141.8) / 50 = 6756.8 / 50 = 135.136
      true range = 1.0 on normal bars, 2.5 on the jump bar (142.3 - 139.8)
      ATR14 = (13 * 1.0 + 2.5) / 14 = 15.5 / 14
    """
    closes = [*linear_closes(200), Decimal("141.8")]
    volumes = [Decimal(1000)] * 200 + [Decimal(1500)]
    return make_bars(closes, volumes=volumes)


def as_of_after(bars: Sequence[Bar], hours: int = 1) -> datetime:
    """A moment shortly after the last bar's (daily) interval has ended."""
    return bars[-1].timestamp + timedelta(days=1, hours=hours)


def backtest_scenario(
    after_entry: list[Decimal] | None = None, entry_close: Decimal = Decimal("142.0")
) -> list[Bar]:
    """breakout_series-like uptrend on 1,000,000 volume, an entry bar, then `after_entry`.

    Default tail: 20 bars rising 0.2/day to 146.0, then two closes at 143.0 (the first
    is below the lowest low of the previous 10 bars, triggering the trailing exit).
    See tests/unit/backtesting/test_engine.py for the hand-derived trade.
    """
    rising = [Decimal("142.0") + Decimal("0.2") * k for k in range(1, 21)]
    tail = after_entry if after_entry is not None else [*rising, Decimal("143.0"), Decimal("143.0")]
    closes = [*linear_closes(200), Decimal("141.8"), entry_close, *tail]
    volume = Decimal(1_000_000)
    volumes = [volume] * 200 + [volume * Decimal("1.5")] + [volume] * (len(closes) - 201)
    return make_bars(closes, volumes=volumes)
