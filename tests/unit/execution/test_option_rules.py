"""Quote-driven option fills and the options entry gate (synthetic quotes)."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.marketpilot.execution.models import OrderSide
from apps.marketpilot.execution.options import (
    OptionSettings,
    check_option_entry,
    entry_risk,
    quote_fill,
)
from apps.marketpilot.risk.limits import AccountRiskState, RiskLimits
from packages.market_data.options import OptionContract

D = Decimal
NOW = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)
CALL = OptionContract.parse("AAPL240621C00190000")
SETTINGS = OptionSettings()
LIMITS = RiskLimits()  # 0.5% risk per trade -> $500 on $100k
STATE = AccountRiskState(
    halted=False,
    equity=D(100_000),
    cash=D(100_000),
    peak_equity=D(100_000),
    previous_equity=D(100_000),
)


BID, ASK, SIZE = D("2.40"), D("2.50"), D(10)


@dataclass
class Quote:
    symbol: str = CALL.symbol
    quoted_at: datetime = NOW - timedelta(minutes=2)
    bid: Decimal = BID
    ask: Decimal = ASK
    bid_size: Decimal = SIZE
    ask_size: Decimal = SIZE


def gate(quantity: int = 1, worst: Decimal = ASK, quote: Quote | None = None, **kw):
    params = {
        "at": NOW,
        "state": STATE,
        "limits": LIMITS,
        "settings": SETTINGS,
        "check_quote_age": True,
    }
    params.update(kw)
    decision, risk = check_option_entry(CALL, quantity, worst, quote or Quote(), **params)
    failures = {c.rule for c in decision.checks if not c.passed}
    assert decision.approved == (not failures)
    return failures, risk, decision


# --- fills ------------------------------------------------------------------------


def test_buy_fills_at_the_ask_and_sell_at_the_bid() -> None:
    assert quote_fill(OrderSide.BUY, 5, None, Quote()).price == D("2.50")
    assert quote_fill(OrderSide.SELL, 5, None, Quote()).price == D("2.40")


@pytest.mark.parametrize(
    ("side", "limit", "quote", "filled"),
    [
        (OrderSide.BUY, D("2.50"), Quote(), True),  # limit equal to the ask fills
        (OrderSide.BUY, D("2.49"), Quote(), False),  # ask above limit
        (OrderSide.SELL, D("2.40"), Quote(), True),
        (OrderSide.SELL, D("2.41"), Quote(), False),  # bid below limit
        (OrderSide.BUY, None, Quote(ask=D(0)), False),  # no ask
        (OrderSide.SELL, None, Quote(bid=D(0)), False),  # nobody bidding
        (OrderSide.BUY, None, Quote(ask_size=D(4)), False),  # not enough displayed size
        (OrderSide.SELL, None, Quote(bid_size=D(4)), False),
        (OrderSide.BUY, None, Quote(ask_size=D(5)), True),  # exactly enough
    ],
)
def test_fill_rules(side, limit, quote, filled) -> None:
    result = quote_fill(side, 5, limit, quote)
    assert result.filled is filled
    assert result.reason


# --- risk ------------------------------------------------------------------------


def test_premium_at_risk_and_breakeven_are_hand_checked() -> None:
    risk = entry_risk(CALL, 2, D("2.50"), SETTINGS)
    assert risk.commission == D("1.30")  # 2 x 0.65
    assert risk.premium_at_risk == D("501.30")  # 2 x 100 x 2.50 + 1.30
    assert risk.breakeven == D("192.50")
    assert risk.as_dict()["multiplier"] == 100


def test_reasonable_buy_is_approved_with_every_rule_reported() -> None:
    failures, risk, decision = gate()
    assert not failures
    assert [c.rule for c in decision.checks] == [
        "account_active",
        "quote_fresh",
        "two_sided_quote",
        "max_spread",
        "min_days_to_expiry",
        "one_position_per_symbol",
        "max_open_positions",
        "per_trade_risk",
        "max_position_size",
        "max_gross_exposure",
        "sufficient_cash",
        "daily_loss_limit",
        "max_drawdown",
    ]
    assert risk.premium_at_risk == D("250.65")


def test_premium_at_risk_is_capped_by_the_per_trade_budget() -> None:
    # 2 contracts: 500 + 1.30 = 501.30 > 500 budget (0.5% of 100k)
    assert gate(quantity=2)[0] == {"per_trade_risk"}
    assert not gate(quantity=1)[0]


def test_stale_quote_blocks_submission_but_not_the_fill_recheck() -> None:
    old = Quote(quoted_at=NOW - timedelta(minutes=16))
    assert gate(quote=old)[0] == {"quote_fresh"}
    assert not gate(quote=old, check_quote_age=False)[0]


def test_future_quote_is_not_fresh() -> None:
    assert gate(quote=Quote(quoted_at=NOW + timedelta(seconds=1)))[0] == {"quote_fresh"}


def test_wide_spread_is_rejected() -> None:
    # bid 2.00 / ask 2.50: spread 0.50 / mid 2.25 = 22.2% > 20%
    assert gate(quote=Quote(bid=D("2.00")))[0] == {"max_spread"}
    # bid 2.10 / ask 2.50: 0.40 / 2.30 = 17.4%
    assert not gate(quote=Quote(bid=D("2.10")))[0]


def test_one_sided_quote_is_rejected() -> None:
    assert gate(quote=Quote(bid=D(0)))[0] == {"two_sided_quote"}


def test_never_buys_on_expiration_day() -> None:
    expiry_morning = datetime(2024, 6, 21, 14, 0, tzinfo=UTC)
    q = Quote(quoted_at=expiry_morning - timedelta(minutes=1))
    assert gate(quote=q, at=expiry_morning)[0] == {"min_days_to_expiry"}
    day_before = datetime(2024, 6, 20, 14, 0, tzinfo=UTC)
    assert not gate(quote=replace(q, quoted_at=day_before), at=day_before)[0]


def test_account_level_limits_apply_to_options() -> None:
    assert "account_active" in gate(state=replace(STATE, halted=True))[0]
    assert "sufficient_cash" in gate(state=replace(STATE, cash=D(200)))[0]
    assert "max_drawdown" in gate(state=replace(STATE, peak_equity=D(112_000)))[0]
    assert "daily_loss_limit" in gate(state=replace(STATE, previous_equity=D(103_000)))[0]
    held = replace(STATE, position_quantities={CALL.symbol: 1})
    assert "one_position_per_symbol" in gate(state=held)[0]


def test_commission_rounds_up_to_the_cent() -> None:
    assert OptionSettings(commission_per_contract=D("0.333")).commission(1) == D("0.34")
    assert OptionSettings(commission_per_contract=D(0)).commission(10) == 0
