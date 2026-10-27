"""Position sizing: the largest whole-share quantity that respects every limit.

Size = min of the quantities allowed by:
  risk       (equity x max_risk_per_trade_pct) / per-share risk at the WORST entry
  position   (equity x max_position_pct) / entry cost per share
  exposure   remaining gross-exposure headroom / entry cost per share
  cash       cash not reserved by working orders / entry cost per share
  liquidity  max_pct_of_avg_volume x average daily volume

Sizing does not approve anything: the result still goes through the risk gate.
"""

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from apps.marketpilot.execution.costs import CostModel
from apps.marketpilot.execution.models import OrderSide
from apps.marketpilot.risk.limits import AccountRiskState, RiskLimits, per_share_risk

_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class SizingResult:
    quantity: int
    limiting_factor: str
    candidates: dict[str, int]


def size_entry(
    *,
    worst_entry_price: Decimal,
    protective_stop: Decimal,
    average_volume: Decimal | None,
    state: AccountRiskState,
    limits: RiskLimits,
    costs: CostModel,
) -> SizingResult:
    if not 0 < protective_stop < worst_entry_price:
        return SizingResult(0, "invalid_stop", {})
    unit_cost = costs.adverse_price(worst_entry_price, OrderSide.BUY) + costs.commission_per_share
    unit_risk = per_share_risk(worst_entry_price, protective_stop, costs) + (
        2 * costs.commission_per_share
    )
    # Reserve the minimum commission on both sides out of the risk budget and cash.
    fixed_fees = 2 * costs.min_commission
    equity = state.equity

    candidates = {
        "risk": _floor(
            (equity * limits.max_risk_per_trade_pct / _HUNDRED - fixed_fees) / unit_risk
        ),
        "position": _floor(equity * limits.max_position_pct / _HUNDRED / unit_cost),
        "exposure": _floor(
            (
                equity * limits.max_gross_exposure_pct / _HUNDRED
                - state.gross_position_value
                - state.pending_entry_cost
            )
            / unit_cost
        ),
        "cash": _floor((state.cash - state.pending_entry_cost - fixed_fees) / unit_cost),
        "liquidity": (
            _floor(average_volume * limits.max_pct_of_avg_volume / _HUNDRED)
            if average_volume
            else 0
        ),
    }
    limiting = min(candidates, key=lambda name: candidates[name])
    return SizingResult(max(candidates[limiting], 0), limiting, candidates)


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))
