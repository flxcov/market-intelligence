"""Core market data models.

Precision: prices, volumes and VWAP are `Decimal`, parsed straight from the
provider's JSON text (never via binary floats), so `185.64` stays exactly
`185.64`. In API responses Pydantic serializes `Decimal` as a JSON string to
preserve that precision.

Time: every timestamp is timezone-aware UTC (see packages.shared.timeutils).
"""

import re
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from packages.shared.timeutils import ensure_utc, parse_utc_bound, utc_now

DEFAULT_MAX_BARS = 10_000
MAX_BARS_HARD_LIMIT = 100_000


class DataFeed(StrEnum):
    """Stock data feeds. OTC is deliberately excluded (separate high-risk category)."""

    IEX = "iex"
    SIP = "sip"
    DELAYED_SIP = "delayed_sip"


class Adjustment(StrEnum):
    """Corporate-action adjustment applied to historical prices."""

    RAW = "raw"
    SPLIT = "split"
    DIVIDEND = "dividend"
    ALL = "all"


class TimeframeUnit(StrEnum):
    MINUTE = "Min"
    HOUR = "Hour"
    DAY = "Day"
    WEEK = "Week"
    MONTH = "Month"


# Approximate interval lengths; used only to flag bars that may still be forming.
_APPROX_UNIT_DURATION = {
    TimeframeUnit.MINUTE: timedelta(minutes=1),
    TimeframeUnit.HOUR: timedelta(hours=1),
    TimeframeUnit.DAY: timedelta(days=1),
    TimeframeUnit.WEEK: timedelta(weeks=1),
    TimeframeUnit.MONTH: timedelta(days=31),
}

_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9]{0,9}(\.[A-Z0-9]{1,4})?")
_TIMEFRAME_RE = re.compile(r"(\d{1,3})(Min|Hour|Day|Week|Month)")


def normalize_symbol(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("symbol must be a string")
    symbol = value.strip().upper()
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError(
            f"invalid symbol {value!r}: expected 1-10 letters/digits starting with a "
            "letter, optionally with a share-class suffix such as BRK.B"
        )
    return symbol


Symbol = Annotated[str, BeforeValidator(normalize_symbol)]


class Timeframe(BaseModel):
    """Bar interval such as 1Min, 15Min, 1Hour, 1Day. Providers may support a subset."""

    model_config = ConfigDict(frozen=True)

    amount: int = Field(ge=1)
    unit: TimeframeUnit

    @classmethod
    def parse(cls, value: str) -> "Timeframe":
        match = _TIMEFRAME_RE.fullmatch(value.strip())
        if match is None:
            raise ValueError(
                f"invalid timeframe {value!r}: expected <amount><unit>, "
                "e.g. 1Min, 15Min, 1Hour, 1Day, 1Week, 1Month"
            )
        return cls(amount=int(match[1]), unit=TimeframeUnit(match[2]))

    @property
    def approx_duration(self) -> timedelta:
        return _APPROX_UNIT_DURATION[self.unit] * self.amount

    def __str__(self) -> str:
        return f"{self.amount}{self.unit.value}"


def _canonical_timeframe(value: object) -> str:
    if isinstance(value, Timeframe):
        return str(value)
    if not isinstance(value, str):
        raise ValueError("timeframe must be a string")
    return str(Timeframe.parse(value))


class Bar(BaseModel):
    """One OHLCV bar. `timestamp` is the provider-reported bar time, in UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: AwareDatetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    trade_count: int | None = Field(default=None, ge=0)
    vwap: Decimal | None = Field(default=None, ge=0)

    @field_validator("timestamp")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _check_ohlc_consistency(self) -> "Bar":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be >= open, close and low")
        if self.low > min(self.open, self.close):
            raise ValueError("low must be <= open and close")
        return self


class BarsRequest(BaseModel):
    """A validated request for historical bars. Also used as the API query model."""

    model_config = ConfigDict(extra="forbid")

    symbol: Symbol = Field(description="Ticker, e.g. AAPL or BRK.B (case-insensitive).")
    timeframe: Annotated[str, BeforeValidator(_canonical_timeframe)] = Field(
        default="1Day", description="Bar interval, e.g. 1Min, 15Min, 1Hour, 1Day."
    )
    start: datetime = Field(
        description="Inclusive start. YYYY-MM-DD (00:00:00 UTC) or ISO 8601 with offset."
    )
    end: datetime = Field(
        description="Inclusive end. YYYY-MM-DD (23:59:59 UTC) or ISO 8601 with offset."
    )
    adjustment: Adjustment = Field(
        default=Adjustment.RAW, description="Corporate-action adjustment for prices."
    )
    feed: DataFeed | None = Field(
        default=None, description="Data feed; defaults to the configured feed."
    )
    max_bars: int = Field(
        default=DEFAULT_MAX_BARS,
        ge=1,
        le=MAX_BARS_HARD_LIMIT,
        description="Upper bound on bars returned; the response is flagged if truncated.",
    )

    # Bounds are truncated to whole seconds: providers take second precision and
    # bars start on whole-second boundaries.
    @field_validator("start", mode="before")
    @classmethod
    def _parse_start(cls, value: object) -> datetime:
        return parse_utc_bound(value, is_end=False).replace(microsecond=0)

    @field_validator("end", mode="before")
    @classmethod
    def _parse_end(cls, value: object) -> datetime:
        return parse_utc_bound(value, is_end=True).replace(microsecond=0)

    @model_validator(mode="after")
    def _check_range(self) -> "BarsRequest":
        if self.start >= self.end:
            raise ValueError("start must be before end")
        if self.start > utc_now():
            raise ValueError("start is in the future; no market data exists for that range")
        return self

    @property
    def parsed_timeframe(self) -> Timeframe:
        return Timeframe.parse(self.timeframe)


class BarSeries(BaseModel):
    """Bars for one symbol plus the provenance needed to interpret them."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: str
    provider: str
    feed: DataFeed
    adjustment: Adjustment
    start: AwareDatetime = Field(description="Requested start (UTC).")
    end: AwareDatetime = Field(description="Requested end (UTC).")
    retrieved_at: AwareDatetime = Field(description="When the provider was queried (UTC).")
    bars: list[Bar]
    truncated: bool = Field(default=False, description="True if more bars existed beyond max_bars.")
    duplicates_removed: int = Field(
        default=0, ge=0, description="Exact duplicate bars dropped during validation."
    )
    last_bar_may_be_incomplete: bool = Field(
        default=False,
        description="True if the last bar's interval had not ended when data was retrieved.",
    )
    notices: list[str] = Field(
        default_factory=list, description="Data-quality, delay and licensing notes."
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def bar_count(self) -> int:
        return len(self.bars)
