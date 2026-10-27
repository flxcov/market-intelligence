from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from packages.market_data.models import Bar, BarsRequest, Timeframe, TimeframeUnit
from packages.shared.timeutils import parse_utc_bound

T0 = datetime(2024, 1, 2, 5, tzinfo=UTC)


def make_bar(**overrides: object) -> Bar:
    fields: dict[str, object] = {
        "timestamp": T0,
        "open": Decimal("100"),
        "high": Decimal("101"),
        "low": Decimal("99"),
        "close": Decimal("100.5"),
        "volume": Decimal("1000"),
    }
    fields.update(overrides)
    return Bar(**fields)


def make_request(**overrides: object) -> BarsRequest:
    fields: dict[str, object] = {"symbol": "AAPL", "start": "2024-01-02", "end": "2024-01-31"}
    fields.update(overrides)
    return BarsRequest(**fields)


# --- symbols -----------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [(" aapl ", "AAPL"), ("brk.b", "BRK.B")])
def test_symbol_is_normalized(raw: str, expected: str) -> None:
    assert make_request(symbol=raw).symbol == expected


@pytest.mark.parametrize("raw", ["", "1ABC", "AAPL;DROP", "TOOLONGSYMBOL", "BTC/USD", "A B"])
def test_invalid_symbols_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationError, match="invalid symbol"):
        make_request(symbol=raw)


# --- timeframes --------------------------------------------------------------


def test_timeframe_parse_round_trips() -> None:
    tf = Timeframe.parse("15Min")
    assert (tf.amount, tf.unit) == (15, TimeframeUnit.MINUTE)
    assert str(tf) == "15Min"
    assert tf.approx_duration == timedelta(minutes=15)


@pytest.mark.parametrize("raw", ["1day", "0Day", "Day", "1D", "5 Min", ""])
def test_invalid_timeframes_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        Timeframe.parse(raw)


# --- bars --------------------------------------------------------------------


def test_bar_keeps_exact_decimal_values() -> None:
    bar = make_bar(close=Decimal("100.10"))
    assert bar.close == Decimal("100.10")
    assert bar.model_dump(mode="json")["close"] == "100.10"


def test_bar_timestamp_is_converted_to_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    bar = make_bar(timestamp=datetime(2024, 1, 2, 0, tzinfo=eastern))
    assert bar.timestamp == T0
    assert bar.timestamp.tzinfo == UTC


def test_bar_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        make_bar(timestamp=datetime(2024, 1, 2, 5))


@pytest.mark.parametrize(
    "overrides",
    [
        {"high": Decimal("99.5")},  # high below open/close
        {"low": Decimal("100.6")},  # low above close
        {"open": Decimal("0")},
        {"close": Decimal("-1")},
        {"volume": Decimal("-5")},
        {"trade_count": -1},
    ],
)
def test_bar_rejects_inconsistent_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_bar(**overrides)


def test_bar_rejects_nan() -> None:
    with pytest.raises(ValidationError):
        make_bar(close=Decimal("NaN"))


# --- date range parsing ------------------------------------------------------


def test_date_only_bounds_cover_full_utc_days() -> None:
    request = make_request(start="2024-01-02", end="2024-01-03")
    assert request.start == datetime(2024, 1, 2, tzinfo=UTC)
    assert request.end == datetime(2024, 1, 3, 23, 59, 59, tzinfo=UTC)


def test_datetime_bounds_with_offsets_are_converted_to_utc() -> None:
    request = make_request(start="2024-01-02T09:30:00-05:00", end="2024-01-02T16:00:00Z")
    assert request.start == datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    assert request.end == datetime(2024, 1, 2, 16, tzinfo=UTC)


def test_bounds_are_truncated_to_seconds() -> None:
    request = make_request(start="2024-01-02T14:30:00.900Z")
    assert request.start == datetime(2024, 1, 2, 14, 30, tzinfo=UTC)


def test_naive_datetime_bound_is_rejected() -> None:
    with pytest.raises(ValidationError, match="UTC offset"):
        make_request(start="2024-01-02T09:30:00")


@pytest.mark.parametrize("raw", ["2024-13-01", "yesterday", "02/01/2024"])
def test_garbage_dates_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        make_request(start=raw)


def test_start_must_precede_end() -> None:
    with pytest.raises(ValidationError, match="start must be before end"):
        make_request(start="2024-02-01", end="2024-01-01")


def test_future_start_is_rejected() -> None:
    future = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
    with pytest.raises(ValidationError, match="future"):
        make_request(start=future, end=future)


@pytest.mark.parametrize("max_bars", [0, 100_001])
def test_max_bars_is_bounded(max_bars: int) -> None:
    with pytest.raises(ValidationError):
        make_request(max_bars=max_bars)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make_request(timeFrame="1Day")


def test_parse_utc_bound_rejects_non_strings() -> None:
    with pytest.raises(ValueError):
        parse_utc_bound(12345, is_end=False)
