"""Order vocabulary shared by the fill simulator, risk gate and persistence."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

PAPER = "PAPER"  # label carried by every simulated account, order, fill and response


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TimeInForce(StrEnum):
    DAY = "day"  # valid for the first eligible session only
    GTC = "gtc"  # good until cancelled


class OrderStatus(StrEnum):
    WORKING = "working"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


@dataclass(frozen=True)
class OrderSpec:
    """The parts of an order that decide whether and how it fills on a bar."""

    side: OrderSide
    order_type: OrderType
    quantity: int
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    # Entry safeguards (from signals): cancel instead of filling if the open is outside.
    cancel_if_open_below: Decimal | None = None
    cancel_if_open_above: Decimal | None = None

    def __post_init__(self) -> None:
        if self.quantity < 1:
            raise ValueError("quantity must be a positive whole number of shares")
        if (self.order_type is OrderType.LIMIT) != (self.limit_price is not None):
            raise ValueError("limit_price is required for, and only for, limit orders")
        if (self.order_type is OrderType.STOP) != (self.stop_price is not None):
            raise ValueError("stop_price is required for, and only for, stop orders")
        for price in (self.limit_price, self.stop_price):
            if price is not None and price <= 0:
                raise ValueError("prices must be positive")
