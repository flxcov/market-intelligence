import random
from dataclasses import replace
from decimal import Decimal

import pytest

from apps.marketpilot.execution.costs import CostModel
from apps.marketpilot.risk.limits import (
    AccountRiskState,
    EntryIntent,
    RiskLimits,
    check_entry,
    check_exit,
)
from apps.marketpilot.risk.position_sizing import size_entry

D = Decimal
LIMITS = RiskLimits()
COSTS = CostModel()  # 7 bps adverse, no commission
STATE = AccountRiskState(
    halted=False,
    equity=D(100_000),
    cash=D(100_000),
    peak_equity=D(100_000),
    previous_equity=D(100_000),
)
# Per-share risk: buy 50 -> 50.04 (50.035 rounded up); stop 48 -> 47.96 (47.9664 down) = 2.08
INTENT = EntryIntent(
    symbol="XYZ",
    quantity=100,
    worst_entry_price=D(50),
    protective_stop=D(48),
    last_price=D("49.5"),
    average_volume=D(1_000_000),
)


def failed(intent=INTENT, state=STATE, limits=LIMITS, costs=COSTS) -> set[str]:
    decision = check_entry(intent, state, limits, costs)
    failures = {c.rule for c in decision.checks if not c.passed}
    assert decision.approved == (not failures)
    return failures


def test_reasonable_entry_is_approved_and_every_rule_is_reported() -> None:
    decision = check_entry(INTENT, STATE, LIMITS, COSTS)
    assert decision.approved
    assert [c.rule for c in decision.checks] == [
        "account_active",
        "stop_below_entry",
        "min_price",
        "one_position_per_symbol",
        "max_open_positions",
        "per_trade_risk",
        "max_position_size",
        "max_gross_exposure",
        "sufficient_cash",
        "liquidity",
        "daily_loss_limit",
        "max_drawdown",
    ]
    risk = next(c for c in decision.checks if c.rule == "per_trade_risk")
    assert risk.detail == "risk 208.00 vs budget 500.00"


@pytest.mark.parametrize(
    ("intent_changes", "state_changes", "expected"),
    [
        ({}, {"halted": True}, {"account_active"}),
        ({"protective_stop": D(50)}, {}, {"stop_below_entry"}),
        ({"last_price": D("4.99")}, {}, {"min_price"}),
        ({}, {"position_quantities": {"XYZ": 5}}, {"one_position_per_symbol"}),
        ({}, {"pending_entry_symbols": frozenset({"XYZ"})}, {"one_position_per_symbol"}),
        (
            {},
            {"position_quantities": {f"S{i}": 1 for i in range(10)}},
            {"max_open_positions"},
        ),
        ({"quantity": 300}, {}, {"per_trade_risk", "max_position_size"}),  # risk 624 > 500
        ({"quantity": 300, "protective_stop": D("49.9")}, {}, {"max_position_size"}),
        ({}, {"gross_position_value": D(96_000)}, {"max_gross_exposure"}),
        ({}, {"cash": D(3000)}, {"sufficient_cash"}),
        ({}, {"pending_entry_cost": D(98_000)}, {"sufficient_cash", "max_gross_exposure"}),
        ({"average_volume": D(5000)}, {}, {"liquidity"}),  # cap 50 shares
        ({"average_volume": None}, {}, {"liquidity"}),
        ({}, {"previous_equity": D(103_000)}, {"daily_loss_limit"}),  # -2.91%
        ({}, {"peak_equity": D(111_112)}, {"max_drawdown"}),  # 10.00% drawdown
    ],
)
def test_each_limit_rejects(intent_changes: dict, state_changes: dict, expected: set) -> None:
    assert failed(replace(INTENT, **intent_changes), replace(STATE, **state_changes)) == expected


def test_daily_loss_just_inside_limit_is_allowed() -> None:
    assert failed(state=replace(STATE, previous_equity=D(102_000))) == set()  # -1.96%


def test_drawdown_exactly_at_limit_blocks() -> None:
    state = replace(STATE, equity=D(90_000), cash=D(90_000), peak_equity=D(100_000))
    assert "max_drawdown" in failed(state=state)


def test_exits_are_allowed_when_halted_but_never_short() -> None:
    state = replace(STATE, halted=True, position_quantities={"XYZ": 10})
    assert check_exit("XYZ", 10, state).approved
    assert not check_exit("XYZ", 11, state).approved
    assert not check_exit("ABC", 1, state).approved


# --- sizing --------------------------------------------------------------------------


def test_sizing_picks_the_binding_limit() -> None:
    result = size_entry(
        worst_entry_price=D(50),
        protective_stop=D(48),
        average_volume=D(1_000_000),
        state=STATE,
        limits=LIMITS,
        costs=COSTS,
    )
    # risk 500 / 2.08 = 240; position 10000 / 50.04 = 199; liquidity 10000; cash 1998
    assert result.candidates == {
        "risk": 240,
        "position": 199,
        "exposure": 1998,
        "cash": 1998,
        "liquidity": 10_000,
    }
    assert (result.quantity, result.limiting_factor) == (199, "position")


def test_sizing_with_wide_stop_is_risk_limited() -> None:
    result = size_entry(
        worst_entry_price=D(50),
        protective_stop=D(40),
        average_volume=D(1_000_000),
        state=STATE,
        limits=LIMITS,
        costs=COSTS,
    )
    assert result.limiting_factor == "risk"
    assert result.quantity == 49  # 500 / (50.04 - 39.97) = 49.65


def test_sizing_invalid_stop_or_no_volume_gives_zero() -> None:
    kwargs = {"state": STATE, "limits": LIMITS, "costs": COSTS}
    assert (
        size_entry(
            worst_entry_price=D(50), protective_stop=D(51), average_volume=D(1), **kwargs
        ).quantity
        == 0
    )
    assert (
        size_entry(
            worst_entry_price=D(50), protective_stop=D(48), average_volume=None, **kwargs
        ).quantity
        == 0
    )


def test_sized_quantity_always_passes_the_quantitative_gate() -> None:
    """Sizing and the gate are independent code; they must agree (seeded random cases)."""
    quantitative = {
        "per_trade_risk",
        "max_position_size",
        "max_gross_exposure",
        "sufficient_cash",
        "liquidity",
    }
    rng = random.Random(7)
    for _ in range(1000):
        costs = CostModel(
            half_spread_bps=D(rng.randint(0, 50)),
            slippage_bps=D(rng.randint(0, 50)),
            commission_per_share=D(rng.choice([0, 1, 5])) / 1000,
            min_commission=D(rng.choice([0, 1])),
        )
        equity = D(rng.randint(1_000, 1_000_000))
        state = AccountRiskState(
            halted=False,
            equity=equity,
            cash=equity * D(rng.randint(10, 100)) / 100,
            peak_equity=equity,
            previous_equity=None,
            gross_position_value=equity * D(rng.randint(0, 80)) / 100,
            pending_entry_cost=D(0),
        )
        entry = D(rng.randint(500, 100_000)) / 100
        stop = entry * D(rng.randint(80, 99)) / 100
        volume = D(rng.randint(100, 5_000_000))
        sized = size_entry(
            worst_entry_price=entry,
            protective_stop=stop,
            average_volume=volume,
            state=state,
            limits=LIMITS,
            costs=costs,
        )
        if sized.quantity < 1:
            continue
        intent = EntryIntent("XYZ", sized.quantity, entry, stop, entry, volume)
        decision = check_entry(intent, state, LIMITS, costs)
        broken = {c.rule for c in decision.checks if not c.passed} & quantitative
        assert broken == set(), (sized, decision.failures)
