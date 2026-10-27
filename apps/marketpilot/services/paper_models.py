"""Request and response models for PAPER trading. Every response says `mode: PAPER`."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from apps.marketpilot.execution.costs import CostModel
from apps.marketpilot.execution.models import (
    PAPER,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from apps.marketpilot.execution.options import OptionSettings
from apps.marketpilot.risk.limits import RiskLimits
from apps.marketpilot.strategies.base import RuleCheck
from packages.market_data.models import DataFeed, Symbol
from packages.market_data.options import ContractSymbol

PaperMode = Literal["PAPER"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


# --- requests ------------------------------------------------------------------------


class CreateAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    starting_cash: Decimal = Field(gt=0, le=Decimal("1000000000"), decimal_places=2)
    feed: DataFeed | None = Field(default=None, description="Defaults to the configured feed.")
    risk_limits: RiskLimits = Field(default_factory=RiskLimits)
    cost_model: CostModel = Field(default_factory=CostModel)
    option_settings: OptionSettings | None = Field(
        default=None,
        description="Options feed, commission and quote limits. Defaults use the configured "
        "options feed.",
    )


class ManualOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: Symbol
    side: OrderSide
    order_type: OrderType = OrderType.MARKET
    quantity: int | None = Field(
        default=None,
        ge=1,
        le=10_000_000,
        description="Buys: omit to size automatically from the risk limits. "
        "Sells: omit to sell the whole position.",
    )
    limit_price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    protective_stop_price: Decimal | None = Field(
        default=None, gt=0, description="Required for buys: defines the trade's risk."
    )
    time_in_force: TimeInForce = TimeInForce.DAY
    client_order_id: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.order_type is OrderType.LIMIT) != (self.limit_price is not None):
            raise ValueError("limit_price is required for, and only for, limit orders")
        if (self.order_type is OrderType.STOP) != (self.stop_price is not None):
            raise ValueError("stop_price is required for, and only for, stop orders")
        if self.side is OrderSide.BUY and self.protective_stop_price is None:
            raise ValueError("buys require protective_stop_price: every entry needs a stop")
        if self.side is OrderSide.SELL and self.protective_stop_price is not None:
            raise ValueError("protective_stop_price applies to buys only")
        return self


class OptionOrderRequest(BaseModel):
    """Buy to open or sell to close a standard (100-share) equity option. Long only."""

    model_config = ConfigDict(extra="forbid")

    symbol: ContractSymbol = Field(description="OCC symbol, e.g. AAPL250117C00200000.")
    side: OrderSide
    quantity: int | None = Field(
        default=None,
        ge=1,
        le=1000,
        description="Contracts. Required for buys; omit on sells to close the whole position.",
    )
    order_type: OrderType = OrderType.LIMIT
    limit_price: Decimal | None = Field(
        default=None, gt=0, decimal_places=2, description="Per-share premium."
    )
    time_in_force: TimeInForce = TimeInForce.DAY
    client_order_id: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.order_type is OrderType.STOP:
            raise ValueError("stop orders are not supported for options")
        if (self.order_type is OrderType.LIMIT) != (self.limit_price is not None):
            raise ValueError("limit_price is required for, and only for, limit orders")
        if self.side is OrderSide.BUY and self.quantity is None:
            raise ValueError("buys require quantity (number of contracts)")
        return self


class SignalOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: int = Field(ge=1)


class ProcessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: AwareDatetime | None = Field(
        default=None, description="Simulate through bars completed by this moment (default now)."
    )


# --- responses -----------------------------------------------------------------------


class _Paper(BaseModel):
    mode: PaperMode = PAPER


class OrderView(_Paper):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    symbol: str
    asset_class: str = "equity"
    multiplier: int = 1
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: int
    limit_price: Decimal | None
    stop_price: Decimal | None
    protective_stop_price: Decimal | None
    cancel_if_open_below: Decimal | None
    cancel_if_open_above: Decimal | None
    signal_id: int | None
    parent_order_id: int | None
    deployment_id: int | None
    client_order_id: str | None
    status: OrderStatus
    status_reason: str | None
    submitted_at: datetime
    active_from: datetime
    reserved_cost: Decimal | None
    risk_checks: list[RuleCheck]
    sizing: dict | None
    filled_bar_at: datetime | None
    closed_at: datetime | None


class OrderResult(_Paper):
    order: OrderView
    accepted: bool
    already_existed: bool = False


class FillView(_Paper):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    symbol: str
    side: OrderSide
    quantity: int
    multiplier: int = 1
    base_price: Decimal
    price: Decimal
    commission: Decimal
    bar_timestamp: datetime
    created_at: datetime


class LedgerEntryView(_Paper):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    amount: Decimal
    symbol: str | None
    quantity: int | None
    price: Decimal | None
    multiplier: int = 1
    fill_id: int | None
    effective_at: datetime


class EquitySnapshotView(_Paper):
    model_config = ConfigDict(from_attributes=True)

    bar_at: datetime
    cash: Decimal
    market_value: Decimal
    equity: Decimal


class PositionView(BaseModel):
    symbol: str
    quantity: int
    multiplier: int = 1
    average_cost: Decimal | None
    cost_basis: Decimal
    mark_price: Decimal | None
    mark_bar_at: datetime | None
    market_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal


class AccountSummary(_Paper):
    id: int
    name: str
    status: str
    feed: str
    starting_cash: Decimal
    cash: Decimal
    processed_through: datetime | None
    created_at: datetime


class AccountView(AccountSummary):
    valued_at: datetime
    equity: Decimal
    market_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    total_return_pct: Decimal
    peak_equity: Decimal
    drawdown_pct: Decimal
    positions: list[PositionView]
    working_orders: int
    risk_limits: RiskLimits
    cost_model: CostModel
    option_settings: OptionSettings
    notices: list[str]


class ProcessEvent(BaseModel):
    session: datetime  # the bar session, quote time or expiry the event happened at
    order_id: int | None
    symbol: str
    outcome: Literal["filled", "cancelled", "expired", "settled"]
    detail: str
    fill_id: int | None = None


class ProcessResult(_Paper):
    account_id: int
    as_of: datetime
    sessions_processed: list[datetime]
    processed_through: datetime | None
    events: list[ProcessEvent]
    equity: Decimal | None
    notices: list[str]


class ReconcileResult(_Paper):
    account_id: int
    balanced: bool
    checks: list[RuleCheck]
