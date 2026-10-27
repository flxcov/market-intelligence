"""Transaction cost model: spread, slippage and commission.

All costs work against the trader. Prices are rounded to a legal tick in the
adverse direction (buys round up, sells round down); commissions round up to
the cent. These are assumptions, not measurements: tune them per account.
"""

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field

from apps.marketpilot.execution.models import OrderSide

CENT = Decimal("0.01")
SUB_PENNY = Decimal("0.0001")
_BPS = Decimal(10_000)


def tick_size(price: Decimal) -> Decimal:
    """U.S. equities quote in cents at or above $1.00, and $0.0001 below."""
    return CENT if price >= 1 else SUB_PENNY


class CostModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    half_spread_bps: Decimal = Field(
        default=Decimal("2"), ge=0, le=500, description="Half the bid/ask spread, in bps."
    )
    slippage_bps: Decimal = Field(
        default=Decimal("5"), ge=0, le=500, description="Extra adverse price impact, in bps."
    )
    commission_per_share: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    min_commission: Decimal = Field(default=Decimal("0"), ge=0, le=100)

    @property
    def adverse_bps(self) -> Decimal:
        return self.half_spread_bps + self.slippage_bps

    def adverse_price(self, base: Decimal, side: OrderSide) -> Decimal:
        """`base` moved against the trader by spread + slippage, on a legal tick."""
        factor = self.adverse_bps / _BPS
        if side is OrderSide.BUY:
            raw = base * (1 + factor)
            return raw.quantize(tick_size(raw), rounding=ROUND_CEILING)
        raw = base * (1 - factor)
        return max(raw.quantize(tick_size(raw), rounding=ROUND_FLOOR), tick_size(raw))

    def commission(self, quantity: int) -> Decimal:
        fee = max(self.commission_per_share * quantity, self.min_commission)
        return fee.quantize(CENT, rounding=ROUND_UP)
