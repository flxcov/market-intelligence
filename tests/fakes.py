"""Test doubles. All data produced here is SYNTHETIC, not real market data."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from packages.market_data.errors import MarketDataError
from packages.market_data.models import Bar, BarSeries, BarsRequest, DataFeed
from packages.market_data.news import NewsArticle, NewsBatch, NewsRequest
from packages.market_data.options import (
    OptionChainRequest,
    OptionChainSnapshot,
    OptionFeed,
    OptionQuote,
)
from packages.market_data.provider import MarketDataProvider

RETRIEVED_AT = datetime(2024, 2, 1, tzinfo=UTC)


def synthetic_daily_bars(count: int, *, first_day: datetime | None = None) -> list[Bar]:
    """`count` consecutive daily bars with simple, valid, invented prices."""
    first = first_day or datetime(2024, 1, 2, 5, tzinfo=UTC)
    bars = []
    for i in range(count):
        base = Decimal("100.10") + Decimal(i)
        bars.append(
            Bar(
                timestamp=first + timedelta(days=i),
                open=base,
                high=base + Decimal("1.25"),
                low=base - Decimal("0.75"),
                close=base + Decimal("0.45"),
                volume=Decimal(1000 + i),
                trade_count=10 + i,
                vwap=base + Decimal("0.2"),
            )
        )
    return bars


class FakeProvider(MarketDataProvider):
    """Returns configured synthetic bars (filtered to the request range) or raises."""

    name = "fake"

    def __init__(
        self,
        bars: list[Bar] | None = None,
        *,
        error: MarketDataError | None = None,
        last_bar_may_be_incomplete: bool = False,
        truncated: bool = False,
        option_quotes: list[OptionQuote] | None = None,
        news: list[NewsArticle] | None = None,
    ) -> None:
        self.news = news or []
        self.news_requests: list[NewsRequest] = []
        self.bars = synthetic_daily_bars(1) if bars is None else bars
        self.option_quotes = option_quotes or []
        self.option_requests: list[OptionChainRequest] = []
        self.error = error
        self.last_bar_may_be_incomplete = last_bar_may_be_incomplete
        self.truncated = truncated
        self.requests: list[BarsRequest] = []

    async def get_bars(self, request: BarsRequest) -> BarSeries:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return BarSeries(
            symbol=request.symbol,
            timeframe=request.timeframe,
            provider=self.name,
            feed=request.feed or DataFeed.IEX,
            adjustment=request.adjustment,
            start=request.start,
            end=request.end,
            retrieved_at=RETRIEVED_AT,
            bars=[b for b in self.bars if request.start <= b.timestamp <= request.end],
            truncated=self.truncated,
            last_bar_may_be_incomplete=self.last_bar_may_be_incomplete,
            notices=["synthetic test data"],
        )

    async def get_option_chain(self, request: OptionChainRequest) -> OptionChainSnapshot:
        self.option_requests.append(request)
        if self.error is not None:
            raise self.error
        return OptionChainSnapshot(
            underlying=request.underlying,
            provider=self.name,
            feed=request.feed or OptionFeed.INDICATIVE,
            retrieved_at=RETRIEVED_AT,
            quotes=[q for q in self.option_quotes if q.contract.root == request.underlying],
            notices=["synthetic test data"],
        )

    async def get_news(self, request: NewsRequest) -> NewsBatch:
        self.news_requests.append(request)
        if self.error is not None:
            raise self.error
        return NewsBatch(
            symbol=request.symbol,
            provider=self.name,
            start=request.start,
            end=request.end,
            retrieved_at=request.end,
            articles=[a for a in self.news if request.start <= a.created_at <= request.end][
                : request.max_articles
            ],
            notices=["synthetic test news"],
        )
