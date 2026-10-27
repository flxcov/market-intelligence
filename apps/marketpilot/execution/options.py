"""PAPER options: quote-driven fills and the options entry gate. Long calls/puts only.

Fills come ONLY from stored option quotes, never from the underlying's price:
  - a buy fills at the displayed ASK, a sell at the displayed BID (the full
    spread is paid; no midpoint optimism);
  - a limit buy fills only if the ask is at or below the limit (sell: bid at or
    above), i.e. the quote must reach the limit;
  - the displayed size on that side must cover the whole order (no partial
    fills, and no filling more contracts than were shown);
  - only quotes stamped AFTER the order was submitted can fill it.

The maximum loss of a long option is the premium paid plus the entry commission,
so that ("premium at risk") is what the per-trade risk budget is checked against.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from apps.marketpilot.execution.costs import CENT
from apps.marketpilot.execution.models import OrderSide
from apps.marketpilot.risk.limits import (
    AccountRiskState,
    RiskDecision,
    RiskLimits,
    account_active_check,
    capital_checks,
    loss_checks,
    per_trade_risk_check,
    slot_checks,
)
from apps.marketpilot.strategies.base import RuleCheck, check
from packages.market_data.options import CONTRACT_MULTIPLIER, OptionContract, OptionFeed


class OptionSettings(BaseModel):
    """Per-account options assumptions. Defaults are cautious, not advice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quote_feed: OptionFeed = Field(
        default=OptionFeed.INDICATIVE, description="Quotes used for fills and marks."
    )
    commission_per_contract: Decimal = Field(default=Decimal("0.65"), ge=0, le=10)
    max_quote_age_minutes: int = Field(
        default=15, ge=1, le=240, description="Newest quote must be this fresh to submit a buy."
    )
    max_spread_pct_of_mid: Decimal = Field(
        default=Decimal("20"), gt=0, le=100, description="(ask - bid) / mid, in percent."
    )
    min_days_to_expiry: int = Field(
        default=1, ge=1, le=365, description="Never buy a contract on its expiration day."
    )

    def commission(self, contracts: int) -> Decimal:
        return (self.commission_per_contract * contracts).quantize(CENT, rounding=ROUND_UP)


class QuoteLike(Protocol):
    """What the fill and risk logic read from a quote (stored record or model)."""

    symbol: str
    quoted_at: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal


@dataclass(frozen=True)
class QuoteFill:
    price: Decimal | None
    reason: str

    @property
    def filled(self) -> bool:
        return self.price is not None


def quote_fill(
    side: OrderSide, quantity: int, limit_price: Decimal | None, quote: QuoteLike
) -> QuoteFill:
    if side is OrderSide.BUY:
        if quote.ask <= 0:
            return QuoteFill(None, "no ask displayed")
        if quote.ask_size < quantity:
            return QuoteFill(None, f"ask size {quote.ask_size} < {quantity} contracts")
        if limit_price is not None and quote.ask > limit_price:
            return QuoteFill(None, f"ask {quote.ask} above limit {limit_price}")
        return QuoteFill(quote.ask, f"bought at the ask {quote.ask}")
    if quote.bid <= 0:
        return QuoteFill(None, "no bid displayed")
    if quote.bid_size < quantity:
        return QuoteFill(None, f"bid size {quote.bid_size} < {quantity} contracts")
    if limit_price is not None and quote.bid < limit_price:
        return QuoteFill(None, f"bid {quote.bid} below limit {limit_price}")
    return QuoteFill(quote.bid, f"sold at the bid {quote.bid}")


@dataclass(frozen=True)
class OptionEntryRisk:
    premium: Decimal  # per share (per unit), the worst entry price
    premium_at_risk: Decimal  # premium x quantity x multiplier + entry commission
    commission: Decimal
    breakeven: Decimal

    def as_dict(self) -> dict[str, str | int]:
        return {
            "premium_per_share": str(self.premium),
            "premium_at_risk": str(self.premium_at_risk),
            "entry_commission": str(self.commission),
            "breakeven_at_expiry": str(self.breakeven),
            "multiplier": CONTRACT_MULTIPLIER,
            "note": "Maximum loss of a long option is the premium paid plus commission; "
            "the breakeven ignores commissions and assumes the position is held to expiry.",
        }


def entry_risk(
    contract: OptionContract, quantity: int, premium: Decimal, settings: OptionSettings
) -> OptionEntryRisk:
    commission = settings.commission(quantity)
    return OptionEntryRisk(
        premium=premium,
        premium_at_risk=premium * quantity * contract.multiplier + commission,
        commission=commission,
        breakeven=contract.breakeven(premium),
    )


def check_option_entry(
    contract: OptionContract,
    quantity: int,
    worst_premium: Decimal,
    quote: QuoteLike,
    *,
    at: datetime,
    state: AccountRiskState,
    limits: RiskLimits,
    settings: OptionSettings,
    check_quote_age: bool,
) -> tuple[RiskDecision, OptionEntryRisk]:
    """Gate a long option buy. `worst_premium` is the most it may pay per share."""
    risk = entry_risk(contract, quantity, worst_premium, settings)
    entry_cost = risk.premium_at_risk - risk.commission
    checks: list[RuleCheck] = [account_active_check(state)]
    if check_quote_age:
        age = at - quote.quoted_at
        checks.append(
            check(
                "quote_fresh",
                timedelta(0) <= age <= timedelta(minutes=settings.max_quote_age_minutes),
                f"quote from {quote.quoted_at.isoformat()} is {_minutes(age)} old vs "
                f"limit {settings.max_quote_age_minutes} min",
            )
        )
    two_sided = quote.bid > 0 and quote.ask > 0
    checks.append(check("two_sided_quote", two_sided, f"bid {quote.bid} / ask {quote.ask}"))
    if two_sided:
        mid = (quote.bid + quote.ask) / 2
        spread_pct = (quote.ask - quote.bid) / mid * 100
        checks.append(
            check(
                "max_spread",
                spread_pct <= settings.max_spread_pct_of_mid,
                f"spread {spread_pct:.2f}% of mid vs limit {settings.max_spread_pct_of_mid}%",
            )
        )
    days = contract.days_to_expiry(at)
    checks.append(
        check(
            "min_days_to_expiry",
            days >= settings.min_days_to_expiry,
            f"{days} day(s) to {contract.expiration} vs minimum {settings.min_days_to_expiry}",
        )
    )
    checks += slot_checks(contract.symbol, state, limits)
    checks.append(per_trade_risk_check(risk.premium_at_risk, state, limits))
    checks += capital_checks(entry_cost, risk.commission, state, limits)
    checks += loss_checks(state, limits)
    return RiskDecision(all(c.passed for c in checks), checks), risk


def _minutes(delta: timedelta) -> str:
    return f"{delta.total_seconds() / 60:.1f} min"
