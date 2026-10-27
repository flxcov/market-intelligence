"""Listed equity options: contract symbols, quotes and chain snapshots.

Only STANDARD contracts are supported: 100 shares of the underlying per
contract, OCC root equal to the underlying ticker. Adjusted contracts (after
splits or special dividends, e.g. root "AAPL1"), index options and roots that
differ from the ticker (e.g. "BRKB" for BRK.B) are skipped at ingestion, because
their deliverable is not 100 shares and settling them as if it were would be
wrong.

OCC symbology (as used by Alpaca, without padding):
    ROOT + YYMMDD + C|P + strike x 1000 as 8 digits
    e.g. AAPL240621C00190000 = AAPL call, expiring 2024-06-21, strike 190.000
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from packages.market_data.models import Symbol

CONTRACT_MULTIPLIER = 100
EASTERN = ZoneInfo("America/New_York")
REGULAR_CLOSE = time(16, 0)
MAX_CHAIN_CONTRACTS = 5000
_OCC_RE = re.compile(r"([A-Z][A-Z0-9]{0,5})(\d{2})(\d{2})(\d{2})([CP])(\d{8})")
_STRIKE_SCALE = Decimal(1000)


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


class OptionFeed(StrEnum):
    OPRA = "opra"  # consolidated options feed; needs an Alpaca subscription
    INDICATIVE = "indicative"  # free feed; Alpaca describes these quotes as indicative


OPTION_FEED_NOTICES = {
    OptionFeed.OPRA: "OPRA feed: consolidated quotes from all U.S. options exchanges.",
    OptionFeed.INDICATIVE: (
        "Indicative feed: Alpaca's free options feed. These quotes are described as "
        "indicative (they may be delayed or derived) rather than firm exchange quotes, so "
        "PAPER fills simulated against them are rough approximations. Check Alpaca's "
        "documentation for the exact definition."
    ),
}


class InvalidContractSymbolError(ValueError):
    pass


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    root: str
    expiration: date
    option_type: OptionType
    strike: Decimal

    @classmethod
    def parse(cls, symbol: str) -> Self:
        text = symbol.strip().upper()
        match = _OCC_RE.fullmatch(text)
        if match is None:
            raise InvalidContractSymbolError(
                f"{symbol!r} is not an OCC option symbol (ROOT + YYMMDD + C/P + 8-digit strike)"
            )
        root, yy, mm, dd, kind, strike = match.groups()
        try:
            expiration = date(2000 + int(yy), int(mm), int(dd))
        except ValueError:
            raise InvalidContractSymbolError(f"{symbol!r} has an invalid expiration date") from None
        strike_value = Decimal(strike) / _STRIKE_SCALE
        if strike_value <= 0:
            raise InvalidContractSymbolError(f"{symbol!r} has a zero strike")
        return cls(
            symbol=text,
            root=root,
            expiration=expiration,
            option_type=OptionType.CALL if kind == "C" else OptionType.PUT,
            strike=strike_value,
        )

    @property
    def multiplier(self) -> int:
        return CONTRACT_MULTIPLIER

    @property
    def expires_at(self) -> datetime:
        """Regular-session close on the expiration date (when exercise is decided)."""
        return regular_close_on(self.expiration)

    def intrinsic_value(self, underlying_price: Decimal) -> Decimal:
        """Per-share value if exercised at `underlying_price` (never negative)."""
        if self.option_type is OptionType.CALL:
            return max(underlying_price - self.strike, Decimal(0))
        return max(self.strike - underlying_price, Decimal(0))

    def breakeven(self, premium: Decimal) -> Decimal:
        """Underlying price at expiry where a long position bought at `premium` breaks even
        (before commissions)."""
        if self.option_type is OptionType.CALL:
            return self.strike + premium
        return self.strike - premium

    def days_to_expiry(self, at: datetime) -> int:
        """Calendar days from `at`'s Eastern date to the expiration date."""
        return (self.expiration - at.astimezone(EASTERN).date()).days


def normalize_contract_symbol(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("contract symbol must be a string")
    return OptionContract.parse(value).symbol


ContractSymbol = Annotated[str, BeforeValidator(normalize_contract_symbol)]


class OptionQuote(BaseModel):
    """One contract's latest quote, as provided. Sizes are in contracts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: ContractSymbol
    quoted_at: AwareDatetime
    bid: Decimal = Field(ge=0)
    ask: Decimal = Field(ge=0)
    bid_size: Decimal = Field(ge=0)
    ask_size: Decimal = Field(ge=0)
    bid_exchange: str | None = None
    ask_exchange: str | None = None
    condition: str | None = None
    last_trade_price: Decimal | None = Field(default=None, gt=0)
    last_trade_at: AwareDatetime | None = None
    implied_volatility: Decimal | None = Field(default=None, ge=0)
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    rho: Decimal | None = None

    @model_validator(mode="after")
    def _not_crossed(self) -> Self:
        if self.bid > 0 and self.ask > 0 and self.ask < self.bid:
            raise ValueError(f"crossed quote: ask {self.ask} below bid {self.bid}")
        return self

    @property
    def contract(self) -> OptionContract:
        return OptionContract.parse(self.symbol)

    @property
    def two_sided(self) -> bool:
        return self.bid > 0 and self.ask > 0

    @property
    def mid(self) -> Decimal | None:
        return (self.bid + self.ask) / 2 if self.two_sided else None

    @property
    def spread_pct_of_mid(self) -> Decimal | None:
        mid = self.mid
        return (self.ask - self.bid) / mid * 100 if mid else None


class OptionChainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    underlying: Symbol
    feed: OptionFeed | None = Field(default=None, description="Defaults to the configured feed.")
    option_type: OptionType | None = None
    expiration_date_gte: date | None = None
    expiration_date_lte: date | None = None
    strike_price_gte: Decimal | None = Field(default=None, gt=0)
    strike_price_lte: Decimal | None = Field(default=None, gt=0)
    max_contracts: int = Field(default=500, ge=1, le=MAX_CHAIN_CONTRACTS)

    @model_validator(mode="after")
    def _ranges(self) -> Self:
        if (
            self.expiration_date_gte
            and self.expiration_date_lte
            and self.expiration_date_gte > self.expiration_date_lte
        ):
            raise ValueError("expiration_date_gte must not be after expiration_date_lte")
        if (
            self.strike_price_gte
            and self.strike_price_lte
            and self.strike_price_gte > self.strike_price_lte
        ):
            raise ValueError("strike_price_gte must not exceed strike_price_lte")
        return self


class SkippedContract(BaseModel):
    symbol: str
    reason: str


class OptionChainSnapshot(BaseModel):
    """Quotes for an underlying's contracts at retrieval time. Nothing here is invented:
    contracts the provider returned without a usable quote are listed in `skipped`."""

    underlying: str
    provider: str
    feed: OptionFeed
    retrieved_at: AwareDatetime
    quotes: list[OptionQuote]
    skipped: list[SkippedContract] = Field(default_factory=list)
    truncated: bool = False
    notices: list[str] = Field(default_factory=list)


def regular_close_on(day: date) -> datetime:
    """16:00 Eastern on `day`, in UTC."""
    return datetime.combine(day, REGULAR_CLOSE, EASTERN).astimezone(UTC)


def eastern_midnight(day: date) -> datetime:
    """How daily bars for `day` are timestamped (midnight Eastern), in UTC."""
    return datetime.combine(day, time(0), EASTERN).astimezone(UTC)
