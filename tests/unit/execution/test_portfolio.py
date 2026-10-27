import random
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.marketpilot.execution.portfolio import AccountingError, LedgerKind, Portfolio

D = Decimal
T = datetime(2024, 1, 2, 15, tzinfo=UTC)


def funded(amount: str = "1000") -> Portfolio:
    portfolio = Portfolio()
    portfolio.deposit(D(amount), T)
    return portfolio


def test_round_trip_with_fees_hand_checked() -> None:
    p = funded("10000")
    p.buy("XYZ", 100, D("50.00"), D("1.00"), T)  # cash 10000 - 5000 - 1 = 4999
    p.sell("XYZ", 100, D("52.50"), D("1.00"), T)  # cash 4999 + 5250 - 1 = 10248

    v = p.valuation({})
    assert v.cash == D("10248.00")
    assert v.realized_pnl == D("250.00")  # (52.50 - 50.00) x 100
    assert v.fees == D("2.00")
    assert v.equity - v.deposits == D("248.00")
    assert v.identity_difference == 0


def test_partial_sells_keep_identity_exact() -> None:
    p = funded("1000")
    p.buy("XYZ", 1, D("10.00"), D(0), T)
    p.buy("XYZ", 2, D("10.01"), D(0), T)  # basis 30.02, average 10.00666...
    assert p.positions["XYZ"].average_cost == D("10.0067")

    p.sell("XYZ", 1, D("11.00"), D(0), T)  # removes 30.02 / 3 = 10.0067 (quantized)
    assert p.positions["XYZ"].realized_pnl == D("0.9933")
    assert p.positions["XYZ"].cost_basis == D("20.0133")

    p.sell("XYZ", 2, D("9.00"), D(0), T)  # closing removes exactly the remaining basis
    position = p.positions["XYZ"]
    assert (position.quantity, position.cost_basis) == (0, D(0))
    assert position.realized_pnl == D("-1.0200")  # 0.9933 + (18 - 20.0133)
    v = p.valuation({})
    assert v.cash == D("998.98")  # 1000 - 30.02 + 11 + 18
    assert v.identity_difference == 0


def test_unrealized_pnl_uses_marks() -> None:
    p = funded("1000")
    p.buy("XYZ", 10, D("20.00"), D(0), T)
    v = p.valuation({"XYZ": D("21.50")})
    assert (v.market_value, v.unrealized_pnl, v.equity) == (D("215.00"), D("15.00"), D("1015.00"))
    assert v.identity_difference == 0


def test_open_position_requires_a_mark() -> None:
    p = funded()
    p.buy("XYZ", 1, D(10), D(0), T)
    with pytest.raises(AccountingError, match="no mark"):
        p.valuation({})


@pytest.mark.parametrize(
    "action",
    [
        lambda p: p.buy("XYZ", 100, D(10), D(1), T),  # 1001 > 1000: no margin
        lambda p: p.sell("XYZ", 1, D(10), D(0), T),  # nothing held: no shorting
        lambda p: p.buy("XYZ", 0, D(10), D(0), T),
        lambda p: p.buy("XYZ", 1, D(0), D(0), T),
        lambda p: p.buy("XYZ", 1, D(10), D(-1), T),
    ],
)
def test_invalid_transactions_are_rejected_without_side_effects(action) -> None:
    p = funded()
    with pytest.raises(AccountingError):
        action(p)
    assert (p.cash, p.fees, p.positions) == (D(1000), D(0), {})


def test_cannot_sell_more_than_held() -> None:
    p = funded()
    p.buy("XYZ", 5, D(10), D(0), T)
    with pytest.raises(AccountingError, match="holding 5"):
        p.sell("XYZ", 6, D(10), D(0), T)


def test_ledger_lines_describe_every_cash_change() -> None:
    p = Portfolio()
    lines = p.deposit(D(1000), T) + p.buy("XYZ", 10, D(20), D("0.50"), T)
    assert [line.kind for line in lines] == [LedgerKind.DEPOSIT, LedgerKind.BUY, LedgerKind.FEE]
    assert sum(line.amount for line in lines) == p.cash == D("799.50")


def test_random_trading_replays_exactly_and_always_balances() -> None:
    """Seeded randomized check of the two core accounting guarantees."""
    rng = random.Random(20240102)
    p = Portfolio()
    ledger = p.deposit(D("100000"), T)
    symbols = ["AAA", "BBB", "CCC"]
    for _ in range(500):
        symbol = rng.choice(symbols)
        price = D(rng.randint(100, 50_000)) / 100
        fee = D(rng.randint(0, 300)) / 100
        held = p.positions.get(symbol)
        if held and held.quantity and rng.random() < 0.5:
            quantity = rng.randint(1, held.quantity)
            ledger += p.sell(symbol, quantity, price, fee, T)
        else:
            quantity = rng.randint(1, 50)
            if price * quantity + fee <= p.cash:
                ledger += p.buy(symbol, quantity, price, fee, T)
        marks = {s: D(rng.randint(100, 50_000)) / 100 for s in symbols}
        assert p.valuation(marks).identity_difference == 0

    rebuilt = Portfolio.replay(ledger)
    assert (rebuilt.cash, rebuilt.deposits, rebuilt.fees) == (p.cash, p.deposits, p.fees)
    assert rebuilt.positions == p.positions
    assert sum(line.amount for line in ledger) == p.cash


def test_replay_rejects_orphan_fee() -> None:
    p = Portfolio()
    lines = p.deposit(D(100), T) + p.buy("XYZ", 1, D(10), D(1), T)
    with pytest.raises(AccountingError, match="fee line"):
        Portfolio.replay([lines[0], lines[2]])
