"""Cash and position accounting for a PAPER account. Pure, exact and replayable.

Every change to cash is a LedgerLine; the portfolio can be rebuilt from its
ledger at any time (`Portfolio.replay`), which is how stored state is reconciled.

Cost basis: each position tracks its TOTAL cost basis rather than a rounded
average price. Selling part of a position removes a proportional share of the
basis (quantized to $0.0001); selling all of it removes exactly what remains.
This keeps the accounting identity exact, with no rounding drift:

    equity - deposits == realized_pnl + unrealized_pnl - fees

Rules: long only, whole units, no margin (cash may never go negative).

Multipliers: a position's value is price x quantity x multiplier. Shares use 1;
standard equity option contracts use 100 (a $2.50 premium costs $250 per
contract). Prices, average costs and marks are always PER UNIT (per share).

Expiry (options): EXPIRE removes a whole position at its settlement value per
unit (intrinsic value, possibly 0). It has no commission and no fill.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

BASIS_QUANTUM = Decimal("0.0001")


class LedgerKind(StrEnum):
    DEPOSIT = "deposit"
    BUY = "buy"
    SELL = "sell"
    FEE = "fee"
    EXPIRE = "expire"


@dataclass(frozen=True)
class LedgerLine:
    kind: LedgerKind
    amount: Decimal  # signed cash change
    effective_at: datetime
    symbol: str | None = None
    quantity: int | None = None
    price: Decimal | None = None
    multiplier: int = 1


class AccountingError(ValueError):
    """A transaction would break an accounting rule (e.g. negative cash)."""


@dataclass
class Position:
    quantity: int = 0
    cost_basis: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    multiplier: int = 1

    @property
    def average_cost(self) -> Decimal | None:
        """Per-unit cost (per share), comparable with the mark price."""
        if self.quantity == 0:
            return None
        return (self.cost_basis / (self.quantity * self.multiplier)).quantize(
            BASIS_QUANTUM, ROUND_HALF_EVEN
        )


@dataclass(frozen=True)
class PositionValuation:
    symbol: str
    quantity: int
    average_cost: Decimal | None
    cost_basis: Decimal
    mark_price: Decimal | None
    market_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    multiplier: int = 1


@dataclass(frozen=True)
class Valuation:
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    deposits: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    positions: list[PositionValuation]

    @property
    def identity_difference(self) -> Decimal:
        """Zero when the books balance (see module docstring)."""
        return (self.equity - self.deposits) - (self.realized_pnl + self.unrealized_pnl - self.fees)


@dataclass
class Portfolio:
    cash: Decimal = Decimal(0)
    deposits: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    positions: dict[str, Position] = field(default_factory=dict)

    def deposit(self, amount: Decimal, at: datetime) -> list[LedgerLine]:
        if amount <= 0:
            raise AccountingError("deposit must be positive")
        self.cash += amount
        self.deposits += amount
        return [LedgerLine(LedgerKind.DEPOSIT, amount, at)]

    def buy(
        self,
        symbol: str,
        quantity: int,
        price: Decimal,
        commission: Decimal,
        at: datetime,
        multiplier: int = 1,
    ) -> list[LedgerLine]:
        _check_trade(quantity, price, commission)
        position = self._position(symbol, multiplier)
        cost = price * quantity * multiplier
        if cost + commission > self.cash:
            raise AccountingError(
                f"insufficient cash: need {cost + commission}, have {self.cash} (no margin)"
            )
        self.positions[symbol] = position
        position.quantity += quantity
        position.cost_basis += cost
        self.cash -= cost + commission
        self.fees += commission
        lines = [LedgerLine(LedgerKind.BUY, -cost, at, symbol, quantity, price, multiplier)]
        if commission:
            lines.append(LedgerLine(LedgerKind.FEE, -commission, at, symbol))
        return lines

    def sell(
        self,
        symbol: str,
        quantity: int,
        price: Decimal,
        commission: Decimal,
        at: datetime,
        multiplier: int = 1,
    ) -> list[LedgerLine]:
        _check_trade(quantity, price, commission)
        position = self._held(symbol, quantity, multiplier)
        proceeds = price * quantity * multiplier
        self._reduce(position, quantity, proceeds)
        self.cash += proceeds - commission
        self.fees += commission
        lines = [LedgerLine(LedgerKind.SELL, proceeds, at, symbol, quantity, price, multiplier)]
        if commission:
            lines.append(LedgerLine(LedgerKind.FEE, -commission, at, symbol))
        return lines

    def expire(
        self, symbol: str, settlement_price: Decimal, at: datetime, multiplier: int
    ) -> list[LedgerLine]:
        """Close a whole expiring position at `settlement_price` per unit (may be 0)."""
        if settlement_price < 0:
            raise AccountingError("settlement price cannot be negative")
        position = self.positions.get(symbol)
        quantity = position.quantity if position else 0
        if quantity < 1:
            raise AccountingError(f"cannot expire {symbol}: no open position")
        self._held(symbol, quantity, multiplier)
        proceeds = settlement_price * quantity * multiplier
        self._reduce(position, quantity, proceeds)
        self.cash += proceeds
        return [
            LedgerLine(
                LedgerKind.EXPIRE, proceeds, at, symbol, quantity, settlement_price, multiplier
            )
        ]

    def _position(self, symbol: str, multiplier: int) -> Position:
        if multiplier < 1:
            raise AccountingError("multiplier must be a positive whole number")
        position = self.positions.get(symbol)
        if position is None or position.quantity == 0:
            # A closed position keeps its realized P&L; reopening may not change units.
            if position is not None and position.multiplier != multiplier:
                raise AccountingError(f"{symbol} was traded with multiplier {position.multiplier}")
            return position or Position(multiplier=multiplier)
        if position.multiplier != multiplier:
            raise AccountingError(
                f"{symbol} is held with multiplier {position.multiplier}, not {multiplier}"
            )
        return position

    def _held(self, symbol: str, quantity: int, multiplier: int) -> Position:
        position = self.positions.get(symbol)
        held = position.quantity if position else 0
        if position is None or quantity > held:
            raise AccountingError(f"cannot sell {quantity} {symbol}: holding {held} (no shorting)")
        if position.multiplier != multiplier:
            raise AccountingError(
                f"{symbol} is held with multiplier {position.multiplier}, not {multiplier}"
            )
        return position

    @staticmethod
    def _reduce(position: Position, quantity: int, proceeds: Decimal) -> None:
        if quantity == position.quantity:
            removed = position.cost_basis
        else:
            removed = (position.cost_basis * quantity / position.quantity).quantize(
                BASIS_QUANTUM, ROUND_HALF_EVEN
            )
        position.quantity -= quantity
        position.cost_basis -= removed
        position.realized_pnl += proceeds - removed

    def valuation(self, marks: Mapping[str, Decimal]) -> Valuation:
        """Mark open positions to `marks` (normally the latest completed close)."""
        rows = []
        for symbol in sorted(self.positions):
            position = self.positions[symbol]
            mark = marks.get(symbol)
            if position.quantity and mark is None:
                raise AccountingError(f"no mark price for open position {symbol}")
            market_value = (
                mark * position.quantity * position.multiplier
                if position.quantity and mark
                else Decimal(0)
            )
            rows.append(
                PositionValuation(
                    symbol=symbol,
                    quantity=position.quantity,
                    average_cost=position.average_cost,
                    cost_basis=position.cost_basis,
                    mark_price=mark,
                    market_value=market_value,
                    unrealized_pnl=market_value - position.cost_basis,
                    realized_pnl=position.realized_pnl,
                    multiplier=position.multiplier,
                )
            )
        market_value = sum((r.market_value for r in rows), Decimal(0))
        return Valuation(
            cash=self.cash,
            market_value=market_value,
            equity=self.cash + market_value,
            deposits=self.deposits,
            realized_pnl=sum((r.realized_pnl for r in rows), Decimal(0)),
            unrealized_pnl=sum((r.unrealized_pnl for r in rows), Decimal(0)),
            fees=self.fees,
            positions=rows,
        )

    @classmethod
    def replay(cls, lines: Iterable[LedgerLine]) -> "Portfolio":
        """Rebuild a portfolio from its ledger, in recorded order.

        A FEE line always directly follows the trade it belongs to, so each trade
        is re-applied together with its commission. EXPIRE lines have no fee.
        """
        portfolio = cls()
        ordered = list(lines)
        for index, line in enumerate(ordered):
            if line.kind is LedgerKind.DEPOSIT:
                portfolio.deposit(line.amount, line.effective_at)
            elif line.kind is LedgerKind.FEE:
                previous = ordered[index - 1] if index else None
                if previous is None or previous.kind not in (LedgerKind.BUY, LedgerKind.SELL):
                    raise AccountingError("fee line without a preceding trade")
            elif line.kind is LedgerKind.EXPIRE:
                if line.symbol is None or line.price is None:
                    raise AccountingError("incomplete expire ledger line")
                position = portfolio.positions.get(line.symbol)
                if position is None or position.quantity != line.quantity:
                    raise AccountingError(f"expire line for {line.symbol} does not match holding")
                portfolio.expire(line.symbol, line.price, line.effective_at, line.multiplier)
            else:
                if line.symbol is None or line.quantity is None or line.price is None:
                    raise AccountingError(f"incomplete {line.kind} ledger line")
                following = ordered[index + 1] if index + 1 < len(ordered) else None
                commission = (
                    -following.amount
                    if following is not None and following.kind is LedgerKind.FEE
                    else Decimal(0)
                )
                apply = portfolio.buy if line.kind is LedgerKind.BUY else portfolio.sell
                apply(
                    line.symbol,
                    line.quantity,
                    line.price,
                    commission,
                    line.effective_at,
                    line.multiplier,
                )
        return portfolio


def _check_trade(quantity: int, price: Decimal, commission: Decimal) -> None:
    if quantity < 1:
        raise AccountingError("quantity must be a positive whole number")
    if price <= 0:
        raise AccountingError("price must be positive")
    if commission < 0:
        raise AccountingError("commission cannot be negative")
