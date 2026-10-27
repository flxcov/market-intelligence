"""Portfolio accounting with contract multipliers and option expiry (hand-checked)."""

import contextlib
import random
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.marketpilot.execution.portfolio import AccountingError, LedgerKind, Portfolio

D = Decimal
T = datetime(2024, 6, 3, 15, tzinfo=UTC)
CALL = "AAPL240621C00190000"


def funded(amount: str = "10000") -> Portfolio:
    portfolio = Portfolio()
    portfolio.deposit(D(amount), T)
    return portfolio


def test_contracts_cost_premium_times_100() -> None:
    p = funded()
    lines = p.buy(CALL, 2, D("2.50"), D("1.30"), T, multiplier=100)
    # 2 contracts x 100 x 2.50 = 500.00, plus 1.30 commission
    assert lines[0].amount == D("-500.00") and lines[0].multiplier == 100
    assert p.cash == D("10000") - D("501.30")
    position = p.positions[CALL]
    assert position.cost_basis == D("500.00")
    assert position.average_cost == D("2.5000")  # per share, comparable with the bid mark

    v = p.valuation({CALL: D("3.10")})  # marked at the bid
    assert v.market_value == D("620.00")
    assert v.unrealized_pnl == D("120.00")
    assert v.positions[0].multiplier == 100
    assert v.identity_difference == 0


def test_sell_to_close_realizes_pnl() -> None:
    p = funded()
    p.buy(CALL, 3, D("2.50"), D("1.95"), T, multiplier=100)
    p.sell(CALL, 1, D("4.00"), D("0.65"), T, multiplier=100)
    pos = p.positions[CALL]
    assert pos.realized_pnl == D("150.00")  # (4.00 - 2.50) x 100
    assert pos.cost_basis == D("500.00")
    assert p.valuation({CALL: D("1.00")}).identity_difference == 0


def test_expire_in_the_money_settles_at_intrinsic() -> None:
    p = funded()
    p.buy(CALL, 2, D("2.50"), D("1.30"), T, multiplier=100)
    [line] = p.expire(CALL, D("5.25"), T, 100)
    assert line.kind is LedgerKind.EXPIRE
    assert line.amount == D("1050.00")  # 5.25 x 2 x 100, no commission
    assert p.positions[CALL].quantity == 0
    assert p.positions[CALL].realized_pnl == D("550.00")  # 1050 - 500
    v = p.valuation({})
    assert v.cash == D("10000") - D("501.30") + D("1050.00")
    assert v.fees == D("1.30")
    assert v.identity_difference == 0


def test_expire_worthless_loses_the_premium_exactly() -> None:
    p = funded()
    p.buy(CALL, 1, D("2.50"), D("0.65"), T, multiplier=100)
    [line] = p.expire(CALL, D(0), T, 100)
    assert line.amount == 0
    assert p.positions[CALL].realized_pnl == D("-250.00")
    assert p.cash == D("10000") - D("250.65")  # max loss = premium + commission


def test_replay_rebuilds_option_activity() -> None:
    p = funded()
    lines = list(p.buy(CALL, 2, D("2.50"), D("1.30"), T, multiplier=100))
    lines += p.sell(CALL, 1, D("3.00"), D("0.65"), T, multiplier=100)
    lines += p.buy("XYZ", 10, D("50"), D(0), T)
    lines += p.expire(CALL, D("1.20"), T, 100)
    rebuilt = Portfolio.replay([*Portfolio().deposit(D("10000"), T), *lines])
    assert rebuilt.cash == p.cash
    assert rebuilt.fees == p.fees
    for symbol, position in p.positions.items():
        assert vars(rebuilt.positions[symbol]) == vars(position)


@pytest.mark.parametrize(
    ("action", "match"),
    [
        (lambda p: p.buy(CALL, 1, D("2"), D(0), T, multiplier=1), "multiplier 100"),
        (lambda p: p.sell(CALL, 1, D("2"), D(0), T, multiplier=1), "multiplier 100"),
        (lambda p: p.sell(CALL, 2, D("2"), D(0), T, multiplier=100), "holding 1"),
        (lambda p: p.expire(CALL, D("-1"), T, 100), "negative"),
        (lambda p: p.expire("OTHER240621C00190000", D(1), T, 100), "no open position"),
        (lambda p: p.buy("X", 1, D("2"), D(0), T, multiplier=0), "multiplier must be"),
    ],
)
def test_invalid_option_actions_are_refused(action, match: str) -> None:
    p = funded()
    p.buy(CALL, 1, D("2.00"), D(0), T, multiplier=100)
    with pytest.raises(AccountingError, match=match):
        action(p)


def test_no_margin_counts_the_multiplier() -> None:
    p = funded("249.99")
    with pytest.raises(AccountingError, match="insufficient cash"):
        p.buy(CALL, 1, D("2.50"), D(0), T, multiplier=100)
    assert CALL not in p.positions  # a refused buy leaves no trace


def test_replay_refuses_an_expire_line_that_does_not_match_the_holding() -> None:
    p = funded()
    lines = list(p.buy(CALL, 2, D("2.50"), D(0), T, multiplier=100))
    [expire] = p.expire(CALL, D(1), T, 100)
    tampered = type(expire)(**{**vars(expire), "quantity": 1})
    with pytest.raises(AccountingError, match="does not match"):
        Portfolio.replay([*Portfolio().deposit(D("10000"), T), *lines, tampered])


def test_random_option_and_share_activity_keeps_identity_exact() -> None:
    rng = random.Random(8)
    for _ in range(50):
        p = funded("100000")
        for _ in range(40):
            symbol, mult = rng.choice([(CALL, 100), ("XYZ", 1)])
            held = p.positions.get(symbol)
            price = D(rng.randint(1, 5000)) / 100
            fee = D(rng.randint(0, 300)) / 100
            if held and held.quantity and rng.random() < 0.4:
                if symbol == CALL and rng.random() < 0.3:
                    p.expire(symbol, D(rng.randint(0, 900)) / 100, T, mult)
                else:
                    p.sell(symbol, rng.randint(1, held.quantity), price, fee, T, mult)
            else:
                with contextlib.suppress(AccountingError):  # e.g. not enough cash
                    p.buy(symbol, rng.randint(1, 5), price, fee, T, mult)
        marks = {CALL: D(rng.randint(0, 900)) / 100, "XYZ": D(rng.randint(1, 9000)) / 100}
        assert p.valuation(marks).identity_difference == 0
        assert p.cash >= 0
