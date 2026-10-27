from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.marketpilot.execution.costs import CostModel, tick_size
from apps.marketpilot.execution.fill_simulator import (
    UnsupportedBarTimestampError,
    session_open,
    simulate_on_bar,
)
from apps.marketpilot.execution.models import OrderSide, OrderSpec, OrderType
from packages.market_data.models import Bar

D = Decimal
COSTS = CostModel()  # 2 bps half-spread + 5 bps slippage = 7 bps adverse
NO_COSTS = CostModel(half_spread_bps=D(0), slippage_bps=D(0))
BUY, SELL = OrderSide.BUY, OrderSide.SELL


def bar(o: str, h: str, low: str, c: str, ts: datetime | None = None) -> Bar:
    return Bar(
        timestamp=ts or datetime(2024, 3, 4, 5, tzinfo=UTC),
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=D(1000),
    )


# --- costs -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "side", "expected"),
    [
        ("100.00", BUY, "100.07"),  # 100 x 1.0007
        ("100.00", SELL, "99.93"),
        ("50.01", BUY, "50.05"),  # 50.045007 rounds UP to the cent
        ("50.01", SELL, "49.97"),  # 49.974993 rounds DOWN
        ("0.50", SELL, "0.4996"),  # sub-dollar: $0.0001 tick, 0.49965 rounds down
    ],
)
def test_adverse_price(base: str, side: OrderSide, expected: str) -> None:
    assert COSTS.adverse_price(D(base), side) == D(expected)


def test_tick_size() -> None:
    assert (tick_size(D("1.00")), tick_size(D("0.9999"))) == (D("0.01"), D("0.0001"))


@pytest.mark.parametrize(
    ("per_share", "minimum", "quantity", "expected"),
    [("0", "0", 100, "0.00"), ("0.005", "0", 101, "0.51"), ("0.005", "1", 10, "1.00")],
)
def test_commission(per_share: str, minimum: str, quantity: int, expected: str) -> None:
    costs = CostModel(commission_per_share=D(per_share), min_commission=D(minimum))
    assert costs.commission(quantity) == D(expected)


# --- session timing ------------------------------------------------------------------


def test_session_open_is_930_eastern() -> None:
    est = bar("1", "1", "1", "1", ts=datetime(2024, 1, 2, 5, tzinfo=UTC))
    edt = bar("1", "1", "1", "1", ts=datetime(2024, 7, 2, 4, tzinfo=UTC))
    assert session_open(est) == datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    assert session_open(edt) == datetime(2024, 7, 2, 13, 30, tzinfo=UTC)


def test_unexpected_bar_timestamp_is_refused() -> None:
    with pytest.raises(UnsupportedBarTimestampError):
        session_open(bar("1", "1", "1", "1", ts=datetime(2024, 1, 2, 14, 30, tzinfo=UTC)))


# --- fill rules ------------------------------------------------------------------------


def order(side: OrderSide, kind: OrderType, **kwargs) -> OrderSpec:
    return OrderSpec(side=side, order_type=kind, quantity=10, **kwargs)


def test_market_buy_fills_at_open_plus_costs() -> None:
    outcome = simulate_on_bar(order(BUY, OrderType.MARKET), bar("100", "105", "95", "101"), COSTS)
    assert (outcome.action, outcome.base_price, outcome.fill_price) == ("fill", D(100), D("100.07"))


@pytest.mark.parametrize(
    ("ohlc", "expected_base"),
    [
        (("99", "101", "98", "100"), "99"),  # opens below the limit: fills at the open
        (("101", "102", "99.5", "100"), "100"),  # trades through the limit intraday
        (("101", "102", "100", "101"), None),  # only TOUCHES the limit: no fill
        (("101", "102", "100.5", "101"), None),  # never reaches it
    ],
)
def test_limit_buy(ohlc: tuple[str, str, str, str], expected_base: str | None) -> None:
    outcome = simulate_on_bar(order(BUY, OrderType.LIMIT, limit_price=D(100)), bar(*ohlc), NO_COSTS)
    assert outcome.base_price == (D(expected_base) if expected_base else None)


def test_limit_sell_needs_trade_through() -> None:
    limit = order(SELL, OrderType.LIMIT, limit_price=D(100))
    assert simulate_on_bar(limit, bar("99", "100", "98", "99"), NO_COSTS).action == "none"
    assert simulate_on_bar(limit, bar("99", "100.5", "98", "99"), NO_COSTS).base_price == D(100)
    assert simulate_on_bar(limit, bar("101", "102", "98", "99"), NO_COSTS).base_price == D(101)


def test_limit_fill_never_worse_than_limit_after_costs() -> None:
    outcome = simulate_on_bar(
        order(BUY, OrderType.LIMIT, limit_price=D(100)), bar("99.99", "101", "99", "100"), COSTS
    )
    assert outcome.fill_price == D(100)  # 99.99 x 1.0007 = 100.06 would exceed the limit


@pytest.mark.parametrize(
    ("ohlc", "expected_base"),
    [
        (("100", "101", "94", "95"), "95"),  # trades down through the stop: fills at stop
        (("90", "92", "89", "91"), "90"),  # gaps below the stop: fills at the worse open
        (("100", "101", "95.01", "100"), None),  # never reaches the stop
    ],
)
def test_sell_stop(ohlc: tuple[str, str, str, str], expected_base: str | None) -> None:
    outcome = simulate_on_bar(order(SELL, OrderType.STOP, stop_price=D(95)), bar(*ohlc), NO_COSTS)
    assert outcome.base_price == (D(expected_base) if expected_base else None)


def test_buy_stop_gap_fills_at_open() -> None:
    stop = order(BUY, OrderType.STOP, stop_price=D(105))
    assert simulate_on_bar(stop, bar("110", "111", "109", "110"), NO_COSTS).base_price == D(110)
    assert simulate_on_bar(stop, bar("100", "106", "99", "104"), NO_COSTS).base_price == D(105)


def test_stop_fill_includes_slippage() -> None:
    outcome = simulate_on_bar(
        order(SELL, OrderType.STOP, stop_price=D(95)), bar("100", "101", "94", "95"), COSTS
    )
    assert outcome.fill_price == D("94.93")  # 95 x 0.9993 = 94.9335 rounds down


@pytest.mark.parametrize(
    ("open_", "expected"), [("94.99", "cancel"), ("105.01", "cancel"), ("100", "fill")]
)
def test_entry_cancel_conditions(open_: str, expected: str) -> None:
    spec = order(BUY, OrderType.MARKET, cancel_if_open_below=D(95), cancel_if_open_above=D(105))
    assert simulate_on_bar(spec, bar(open_, "106", "94", "100"), NO_COSTS).action == expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"order_type": OrderType.LIMIT},
        {"order_type": OrderType.STOP},
        {"order_type": OrderType.MARKET, "limit_price": D(1)},
        {"order_type": OrderType.MARKET, "quantity": 0},
        {"order_type": OrderType.LIMIT, "limit_price": D(-1)},
    ],
)
def test_invalid_order_specs(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        OrderSpec(**{"side": BUY, "quantity": 1, **kwargs})
