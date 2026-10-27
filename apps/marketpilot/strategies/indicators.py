"""Technical indicators in exact Decimal arithmetic.

Each function computes ONE value from exactly the window it is given; callers
slice the window explicitly. This makes it obvious which bars feed each value,
which is how look-ahead bugs are avoided.
"""

from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise

from packages.market_data.models import Bar


def mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("mean of an empty window")
    return sum(values, Decimal(0)) / len(values)


def simple_moving_average(closes: Sequence[Decimal], period: int) -> Decimal:
    """Average of the last `period` values."""
    if len(closes) < period:
        raise ValueError(f"need {period} values, got {len(closes)}")
    return mean(closes[-period:])


def true_range(bar: Bar, previous_close: Decimal) -> Decimal:
    """Largest of: today's range, and the gaps from yesterday's close to today's high/low."""
    return max(
        bar.high - bar.low,
        abs(bar.high - previous_close),
        abs(bar.low - previous_close),
    )


def average_true_range(bars: Sequence[Bar], period: int) -> Decimal:
    """Simple average of the last `period` true ranges (needs period + 1 bars).

    Uses a simple mean rather than Wilder's smoothing so the value depends only
    on the visible window, never on how much history happened to be loaded.
    """
    if len(bars) < period + 1:
        raise ValueError(f"need {period + 1} bars, got {len(bars)}")
    window = bars[-(period + 1) :]
    return mean([true_range(bar, prev.close) for prev, bar in pairwise(window)])


def highest_high(bars: Sequence[Bar]) -> Decimal:
    return max(bar.high for bar in bars)


def lowest_low(bars: Sequence[Bar]) -> Decimal:
    return min(bar.low for bar in bars)
