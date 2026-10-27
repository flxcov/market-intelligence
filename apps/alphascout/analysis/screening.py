"""Universe screening. Filters are configurable experiments, not safety criteria."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

MAJOR_EXCHANGES = frozenset({"Nasdaq", "NYSE", "NYSE American", "NYSE Arca", "Cboe BZX"})


class ScreenFilters(BaseModel):
    min_price: Decimal = Field(default=Decimal("5"), ge=0)
    min_market_cap: Decimal = Field(default=Decimal("50000000"), ge=0)
    max_market_cap: Decimal = Field(default=Decimal("2000000000"), gt=0)
    min_avg_dollar_volume: Decimal = Field(default=Decimal("1000000"), ge=0)
    max_price_age_days: int = Field(default=4, ge=1, le=30)


class ScreenInput(BaseModel):
    ticker: str
    name: str | None
    exchanges: list[str]
    price: Decimal | None
    price_bar_at: datetime | None
    avg_dollar_volume: Decimal | None
    shares_outstanding: Decimal | None
    as_of: datetime


class ScreenResult(BaseModel):
    ticker: str
    name: str | None
    passed: bool
    market_cap: Decimal | None
    price: Decimal | None
    price_bar_at: datetime | None
    avg_dollar_volume: Decimal | None
    exchanges: list[str]
    reasons: list[str]  # why it failed, or missing data
    flags: list[str]  # high-risk categories, reported even when it passes


def screen(item: ScreenInput, f: ScreenFilters) -> ScreenResult:
    reasons, flags = [], []
    market_cap = None
    if item.price is None or item.price_bar_at is None:
        reasons.append("no stored price: ingest raw daily bars first")
    else:
        age_days = (item.as_of - item.price_bar_at).total_seconds() / 86400 - 1
        if age_days > f.max_price_age_days:
            reasons.append(f"price is stale (bar of {item.price_bar_at.date()})")
        if item.price < f.min_price:
            reasons.append(f"price {item.price} below {f.min_price}")
        if item.shares_outstanding is None:
            reasons.append("shares outstanding not reported in SEC data")
        else:
            market_cap = (item.price * item.shares_outstanding).quantize(Decimal(1))
            if not f.min_market_cap <= market_cap <= f.max_market_cap:
                reasons.append(
                    f"market cap {market_cap} outside {f.min_market_cap}-{f.max_market_cap}"
                )
    if item.avg_dollar_volume is None:
        reasons.append("no volume history")
    elif item.avg_dollar_volume < f.min_avg_dollar_volume:
        reasons.append(
            f"average dollar volume {item.avg_dollar_volume.quantize(Decimal(1))} too low"
        )
    if not item.exchanges:
        flags.append("no exchange listed in SEC data (possibly OTC or delisted)")
        reasons.append("not listed on a major U.S. exchange")
    elif not set(item.exchanges) & MAJOR_EXCHANGES:
        flags.append(f"exchange {', '.join(item.exchanges)} is high-risk (e.g. OTC)")
        reasons.append("not listed on a major U.S. exchange")
    if market_cap is not None and market_cap < Decimal("300000000"):
        flags.append("microcap: higher volatility and wider spreads")
    return ScreenResult(
        ticker=item.ticker,
        name=item.name,
        passed=not reasons,
        market_cap=market_cap,
        price=item.price,
        price_bar_at=item.price_bar_at,
        avg_dollar_volume=item.avg_dollar_volume,
        exchanges=item.exchanges,
        reasons=reasons,
        flags=flags,
    )
