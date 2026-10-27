"""The mandatory pre-trade risk gate for PAPER orders.

Every order passes through `check_entry` or `check_exit` before it is accepted.
All checks are evaluated and reported (not just the first failure), so a
rejection explains itself. Limits are fixed when an account is created and are
never loosened by code.

Default limits are a cautious starting point for experiments, not advice.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from apps.marketpilot.execution.costs import CostModel
from apps.marketpilot.execution.models import OrderSide
from apps.marketpilot.strategies.base import RuleCheck, check

_HUNDRED = Decimal(100)


class RiskLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_risk_per_trade_pct: Decimal = Field(default=Decimal("0.5"), gt=0, le=5)
    max_position_pct: Decimal = Field(default=Decimal("10"), gt=0, le=100)
    max_gross_exposure_pct: Decimal = Field(
        default=Decimal("100"), gt=0, le=100, description="100 = fully invested, no leverage."
    )
    max_open_positions: int = Field(default=10, ge=1, le=100)
    max_daily_loss_pct: Decimal = Field(default=Decimal("2"), gt=0, le=50)
    max_drawdown_pct: Decimal = Field(default=Decimal("10"), gt=0, le=90)
    max_pct_of_avg_volume: Decimal = Field(default=Decimal("1"), gt=0, le=100)
    min_price: Decimal = Field(default=Decimal("5"), ge=0)
    avg_volume_lookback: int = Field(default=20, ge=1, le=250)


@dataclass(frozen=True)
class AccountRiskState:
    halted: bool
    equity: Decimal
    cash: Decimal
    peak_equity: Decimal
    previous_equity: Decimal | None  # equity at the previous processed session close
    position_quantities: dict[str, int] = field(default_factory=dict)  # open positions only
    gross_position_value: Decimal = Decimal(0)
    pending_entry_symbols: frozenset[str] = frozenset()
    pending_entry_cost: Decimal = Decimal(0)  # cash reserved by working buy orders


@dataclass(frozen=True)
class EntryIntent:
    symbol: str
    quantity: int
    worst_entry_price: Decimal  # highest price the order may fill at, before costs
    protective_stop: Decimal
    last_price: Decimal
    average_volume: Decimal | None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    checks: list[RuleCheck]

    @property
    def failures(self) -> list[str]:
        return [f"{c.rule}: {c.detail}" for c in self.checks if not c.passed]


def per_share_risk(worst_entry: Decimal, stop: Decimal, costs: CostModel) -> Decimal:
    """Price loss per share if bought at the worst entry and stopped out, after spread
    and slippage. Commissions are NOT included; callers add them exactly once."""
    entry = costs.adverse_price(worst_entry, OrderSide.BUY)
    exit_ = costs.adverse_price(stop, OrderSide.SELL)
    return entry - exit_


def check_entry(
    intent: EntryIntent, state: AccountRiskState, limits: RiskLimits, costs: CostModel
) -> RiskDecision:
    q = intent.quantity
    entry_cost = costs.adverse_price(intent.worst_entry_price, OrderSide.BUY) * q
    commission = costs.commission(q)

    checks = [account_active_check(state)]
    stop_ok = 0 < intent.protective_stop < intent.worst_entry_price
    checks.append(
        check(
            "stop_below_entry",
            stop_ok,
            f"stop {intent.protective_stop} vs worst entry {intent.worst_entry_price}",
        )
    )
    checks.append(
        check(
            "min_price",
            intent.last_price >= limits.min_price,
            f"last price {intent.last_price} vs minimum {limits.min_price}",
        )
    )
    checks += slot_checks(intent.symbol, state, limits)
    if stop_ok:
        risk = per_share_risk(intent.worst_entry_price, intent.protective_stop, costs) * q
        risk += commission * 2
        checks.append(per_trade_risk_check(risk, state, limits))
    checks += capital_checks(entry_cost, commission, state, limits)
    if intent.average_volume is None or intent.average_volume <= 0:
        checks.append(check("liquidity", False, "no volume history for this symbol"))
    else:
        volume_cap = intent.average_volume * limits.max_pct_of_avg_volume / _HUNDRED
        checks.append(
            check(
                "liquidity",
                q <= volume_cap,
                f"{q} shares vs {limits.max_pct_of_avg_volume}% of avg volume "
                f"({_money(volume_cap)})",
            )
        )
    checks += loss_checks(state, limits)
    return RiskDecision(all(c.passed for c in checks), checks)


# Building blocks shared with other entry gates (e.g. options). Each returns checks
# with stable rule names, so rejections read the same whatever was traded.


def account_active_check(state: AccountRiskState) -> RuleCheck:
    return check("account_active", not state.halted, "halted" if state.halted else "active")


def slot_checks(symbol: str, state: AccountRiskState, limits: RiskLimits) -> list[RuleCheck]:
    open_count = len(state.position_quantities) + len(state.pending_entry_symbols)
    duplicate = symbol in state.position_quantities or symbol in state.pending_entry_symbols
    return [
        check(
            "one_position_per_symbol",
            not duplicate,
            "already held or pending" if duplicate else "no existing position or entry order",
        ),
        check(
            "max_open_positions",
            open_count + 1 <= limits.max_open_positions,
            f"{open_count} open/pending + 1 vs limit {limits.max_open_positions}",
        ),
    ]


def per_trade_risk_check(risk: Decimal, state: AccountRiskState, limits: RiskLimits) -> RuleCheck:
    budget = state.equity * limits.max_risk_per_trade_pct / _HUNDRED
    return check(
        "per_trade_risk", risk <= budget, f"risk {_money(risk)} vs budget {_money(budget)}"
    )


def capital_checks(
    entry_cost: Decimal, commission: Decimal, state: AccountRiskState, limits: RiskLimits
) -> list[RuleCheck]:
    position_cap = state.equity * limits.max_position_pct / _HUNDRED
    exposure = state.gross_position_value + state.pending_entry_cost + entry_cost
    exposure_cap = state.equity * limits.max_gross_exposure_pct / _HUNDRED
    available = state.cash - state.pending_entry_cost
    return [
        check(
            "max_position_size",
            entry_cost <= position_cap,
            f"cost {_money(entry_cost)} vs cap {_money(position_cap)}",
        ),
        check(
            "max_gross_exposure",
            exposure <= exposure_cap,
            f"exposure {_money(exposure)} vs cap {_money(exposure_cap)}",
        ),
        check(
            "sufficient_cash",
            entry_cost + commission <= available,
            f"need {_money(entry_cost + commission)}, available {_money(available)} (no margin)",
        ),
    ]


def loss_checks(state: AccountRiskState, limits: RiskLimits) -> list[RuleCheck]:
    checks = []
    if state.previous_equity:
        change_pct = (state.equity - state.previous_equity) / state.previous_equity * _HUNDRED
        checks.append(
            check(
                "daily_loss_limit",
                change_pct > -limits.max_daily_loss_pct,
                f"last session {_pct(change_pct)} vs limit -{limits.max_daily_loss_pct}%",
            )
        )
    drawdown_pct = (state.peak_equity - state.equity) / state.peak_equity * _HUNDRED
    checks.append(
        check(
            "max_drawdown",
            drawdown_pct < limits.max_drawdown_pct,
            f"drawdown {_pct(drawdown_pct)} vs limit {limits.max_drawdown_pct}%",
        )
    )
    return checks


def check_exit(symbol: str, quantity: int, state: AccountRiskState) -> RiskDecision:
    """Exits only reduce risk, so they are allowed even when halted or past limits."""
    held = state.position_quantities.get(symbol, 0)
    checks = [
        check(
            "closes_existing_position",
            0 < quantity <= held,
            f"sell {quantity} vs held {held} (no short selling)",
        )
    ]
    return RiskDecision(all(c.passed for c in checks), checks)


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _pct(value: Decimal) -> str:
    return f"{value:.2f}%"
