"""API response models for stored data (persistence-specific views)."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, computed_field

from apps.marketpilot.strategies.base import RuleCheck
from packages.database.models import IngestionStatus
from packages.market_data.models import Adjustment, Bar, DataFeed
from packages.market_data.options import SkippedContract


class StoredBar(Bar):
    """A bar read from the database, with its provenance."""

    model_config = ConfigDict(from_attributes=True)

    provider: str
    retrieved_at: datetime
    revision: int


class StoredBarSeries(BaseModel):
    source: str = "database"
    symbol: str
    timeframe: str
    feed: DataFeed
    adjustment: Adjustment
    start: datetime
    end: datetime
    bars: list[StoredBar]
    truncated: bool
    notices: list[str]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def bar_count(self) -> int:
        return len(self.bars)


class IngestionRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: IngestionStatus
    provider: str
    symbol: str
    timeframe: str
    feed: str | None
    adjustment: str
    range_start: datetime
    range_end: datetime
    started_at: datetime
    finished_at: datetime | None
    bars_received: int | None
    bars_skipped_incomplete: int | None
    inserted: int | None
    updated: int | None
    unchanged: int | None
    truncated: bool | None
    error_type: str | None
    error_message: str | None


class StoredSignal(BaseModel):
    """A signal as recorded, with the strategy config that produced it."""

    id: int
    strategy: str
    strategy_version: str
    parameters_hash: str
    symbol: str
    timeframe: str
    feed: str
    adjustment: str
    side: str
    status: str
    signal_bar_at: datetime
    evaluated_at: datetime
    data_retrieved_at: datetime | None
    created_at: datetime
    reference_price: Decimal
    stop_price: Decimal
    cancel_if_open_below: Decimal
    cancel_if_open_above: Decimal
    expires_after_bars: int
    trailing_exit_lookback_bars: int
    time_stop_bars: int
    atr: Decimal
    risk_per_share: Decimal
    score_components: dict[str, Decimal | None]
    rule_checks: list[RuleCheck]


class SeriesCoverageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    timeframe: str
    feed: str
    adjustment: str
    bars: int
    first_bar: datetime
    last_bar: datetime
    last_retrieved_at: datetime


class StoredOptionQuote(BaseModel):
    symbol: str
    underlying: str
    option_type: str
    strike: Decimal
    expiration: date
    feed: str
    provider: str
    quoted_at: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    mid: Decimal | None
    spread_pct_of_mid: Decimal | None
    last_trade_price: Decimal | None
    last_trade_at: datetime | None
    implied_volatility: Decimal | None
    greeks: dict[str, Decimal] | None
    retrieved_at: datetime

    @classmethod
    def from_record(cls, record) -> "StoredOptionQuote":
        two_sided = record.bid > 0 and record.ask > 0
        mid = (record.bid + record.ask) / 2 if two_sided else None
        return cls(
            symbol=record.symbol,
            underlying=record.underlying,
            option_type=record.option_type,
            strike=record.strike,
            expiration=record.expiration,
            feed=record.feed,
            provider=record.provider,
            quoted_at=record.quoted_at,
            bid=record.bid,
            ask=record.ask,
            bid_size=record.bid_size,
            ask_size=record.ask_size,
            mid=mid,
            spread_pct_of_mid=(
                ((record.ask - record.bid) / mid * 100).quantize(Decimal("0.01")) if mid else None
            ),
            last_trade_price=record.last_trade_price,
            last_trade_at=record.last_trade_at,
            implied_volatility=record.implied_volatility,
            greeks={k: Decimal(v) for k, v in record.greeks.items()} if record.greeks else None,
            retrieved_at=record.retrieved_at,
        )


class StoredOptionChain(BaseModel):
    underlying: str
    feed: str
    as_of: datetime
    quotes: list[StoredOptionQuote]
    notices: list[str]


class OptionChainIngestView(BaseModel):
    underlying: str
    feed: str
    provider: str
    retrieved_at: datetime
    received: int
    inserted: int
    unchanged: int
    conflicting: int
    skipped: list[SkippedContract]
    truncated: bool
    notices: list[str]
