from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from packages.market_data.errors import DataValidationError
from packages.market_data.models import Bar
from packages.market_data.validation import normalize_bars

START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 1, 31, 23, 59, 59, tzinfo=UTC)


def bar(day: int, close: str = "100.5") -> Bar:
    return Bar(
        timestamp=datetime(2024, 1, day, 5, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal(close),
        volume=Decimal("1000"),
    )


def test_bars_are_sorted_chronologically() -> None:
    bars, duplicates = normalize_bars([bar(3), bar(1), bar(2)], start=START, end=END)
    assert [b.timestamp.day for b in bars] == [1, 2, 3]
    assert duplicates == 0


def test_exact_duplicates_are_removed_and_counted() -> None:
    bars, duplicates = normalize_bars([bar(1), bar(2), bar(1)], start=START, end=END)
    assert len(bars) == 2
    assert duplicates == 1


def test_decimal_equivalent_duplicates_count_as_exact() -> None:
    _, duplicates = normalize_bars([bar(1, "100.5"), bar(1, "100.50")], start=START, end=END)
    assert duplicates == 1


def test_conflicting_duplicates_are_rejected() -> None:
    with pytest.raises(DataValidationError, match="conflicting"):
        normalize_bars([bar(1, "100.5"), bar(1, "100.6")], start=START, end=END)


@pytest.mark.parametrize("offset", [timedelta(seconds=-1), timedelta(days=31)])
def test_bars_outside_requested_range_are_rejected(offset: timedelta) -> None:
    outlier = bar(1).model_copy(update={"timestamp": START + offset})
    with pytest.raises(DataValidationError, match="outside the requested range"):
        normalize_bars([outlier], start=START, end=END)


def test_empty_input_is_valid() -> None:
    assert normalize_bars([], start=START, end=END) == ([], 0)
