from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.marketpilot.strategies.indicators import (
    average_true_range,
    highest_high,
    lowest_low,
    mean,
    simple_moving_average,
    true_range,
)
from packages.market_data.models import Bar

D = Decimal


def bar(high: str, low: str, close: str) -> Bar:
    return Bar(
        timestamp=datetime(2024, 1, 2, tzinfo=UTC),
        open=D(close),
        high=D(high),
        low=D(low),
        close=D(close),
        volume=D(1),
    )


def test_sma_uses_only_the_last_period_values() -> None:
    assert simple_moving_average([D(100), D(1), D(2), D(3)], 3) == D(2)


def test_sma_is_exact_in_decimal() -> None:
    assert simple_moving_average([D("0.1"), D("0.2")], 2) == D("0.15")


def test_sma_requires_enough_values() -> None:
    with pytest.raises(ValueError, match="need 3"):
        simple_moving_average([D(1), D(2)], 3)


def test_mean_rejects_empty_window() -> None:
    with pytest.raises(ValueError):
        mean([])


@pytest.mark.parametrize(
    ("high", "low", "close", "previous_close", "expected"),
    [
        ("11", "9", "10", "10", "2"),  # plain range
        ("15", "13", "14", "10", "5"),  # gap up: high - previous close
        ("7", "5", "6", "10", "5"),  # gap down: previous close - low
    ],
)
def test_true_range(high: str, low: str, close: str, previous_close: str, expected: str) -> None:
    assert true_range(bar(high, low, close), D(previous_close)) == D(expected)


def test_atr_averages_last_period_true_ranges() -> None:
    bars = [
        bar("200", "100", "150"),  # outside the window; must not matter
        bar("11", "9", "10"),
        bar("11", "9", "10"),  # TR 2
        bar("15", "13", "14"),  # TR 5 (gap up from 10)
    ]
    assert average_true_range(bars, 2) == D("3.5")


def test_atr_requires_period_plus_one_bars() -> None:
    with pytest.raises(ValueError, match="need 3 bars"):
        average_true_range([bar("11", "9", "10")] * 2, 2)


def test_extremes() -> None:
    bars = [bar("11", "9", "10"), bar("15", "13", "14"), bar("7", "5", "6")]
    assert (highest_high(bars), lowest_low(bars)) == (D(15), D(5))
