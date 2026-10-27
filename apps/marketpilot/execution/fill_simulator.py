"""Decides what happens to one working order on one daily bar. Pure and conservative.

With only daily OHLC we do not know the intraday path, so every rule picks the
outcome that is least favourable to the trader:

  market  fills at the open.
  limit   buy: fills at the open if the open is strictly below the limit,
          otherwise at the limit if the low is strictly below it. Merely touching
          the limit is NOT a fill (the order may have been behind others in queue).
          Sell is the mirror image.
  stop    sell: triggers if the low reaches the stop; if the open is already at
          or below the stop (a gap), fills at the open, which is worse (gap risk).
          Buy is the mirror image.

Spread and slippage are then applied against the trader. A limit order never
fills at a price worse than its limit.

Session timing: daily bars are assumed to be stamped at midnight U.S. Eastern
(04:00Z or 05:00Z), with the regular session opening 9h30m later. Bars with any
other timestamp are refused rather than silently mis-timed.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from apps.marketpilot.execution.costs import CostModel
from apps.marketpilot.execution.models import OrderSide, OrderSpec, OrderType
from packages.market_data.models import Bar

SESSION_OPEN_OFFSET = timedelta(hours=9, minutes=30)
_MIDNIGHT_EASTERN_UTC_HOURS = (4, 5)  # EDT, EST


class UnsupportedBarTimestampError(ValueError):
    pass


def session_open(bar: Bar) -> datetime:
    """Approximate regular-session open for a daily bar."""
    ts = bar.timestamp
    if ts.hour not in _MIDNIGHT_EASTERN_UTC_HOURS or ts.minute or ts.second:
        raise UnsupportedBarTimestampError(
            f"daily bar at {ts.isoformat()} is not stamped at midnight U.S. Eastern; "
            "session timing cannot be inferred"
        )
    return ts + SESSION_OPEN_OFFSET


@dataclass(frozen=True)
class BarOutcome:
    action: Literal["fill", "cancel", "none"]
    base_price: Decimal | None = None  # price before costs
    fill_price: Decimal | None = None  # price after spread/slippage
    reason: str = ""


NO_FILL = BarOutcome("none")


def simulate_on_bar(order: OrderSpec, bar: Bar, costs: CostModel) -> BarOutcome:
    if order.cancel_if_open_below is not None and bar.open < order.cancel_if_open_below:
        return BarOutcome("cancel", reason=f"open {bar.open} below {order.cancel_if_open_below}")
    if order.cancel_if_open_above is not None and bar.open > order.cancel_if_open_above:
        return BarOutcome("cancel", reason=f"open {bar.open} above {order.cancel_if_open_above}")

    base = _base_price(order, bar)
    if base is None:
        return NO_FILL
    price = costs.adverse_price(base, order.side)
    if order.order_type is OrderType.LIMIT:
        assert order.limit_price is not None
        price = (
            min(price, order.limit_price)
            if order.side is OrderSide.BUY
            else max(price, order.limit_price)
        )
    return BarOutcome("fill", base_price=base, fill_price=price)


def _base_price(order: OrderSpec, bar: Bar) -> Decimal | None:
    buy = order.side is OrderSide.BUY
    if order.order_type is OrderType.MARKET:
        return bar.open
    if order.order_type is OrderType.LIMIT:
        limit = order.limit_price
        assert limit is not None
        if buy:
            if bar.open < limit:
                return bar.open
            return limit if bar.low < limit else None
        if bar.open > limit:
            return bar.open
        return limit if bar.high > limit else None
    stop = order.stop_price
    assert stop is not None
    if buy:
        if bar.open >= stop:
            return bar.open
        return stop if bar.high >= stop else None
    if bar.open <= stop:
        return bar.open
    return stop if bar.low <= stop else None
