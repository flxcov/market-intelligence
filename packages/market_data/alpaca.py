"""Alpaca Market Data API v2 adapter for historical stock bars.

API contract (taken from Alpaca's official alpaca-py SDK source, because
docs.alpaca.markets was unreachable from the build environment; confirm against
https://docs.alpaca.markets when credentials are available):

    GET {base_url}/v2/stocks/bars
      symbols, timeframe, start, end (RFC 3339), limit (<= 10000 per page),
      adjustment (raw|split|dividend|all), feed (iex|sip|delayed_sip|...),
      sort (asc|desc), page_token
    Auth headers: APCA-API-KEY-ID, APCA-API-SECRET-KEY
    200 body: {"bars": {"AAPL": [{"t", "o", "h", "l", "c", "v", "n", "vw"}, ...]},
               "next_page_token": "..." | null}

    GET {base_url}/v1beta1/options/snapshots/{underlying}
      feed (opra|indicative), type (call|put), strike_price_gte, strike_price_lte,
      expiration_date_gte, expiration_date_lte (YYYY-MM-DD), limit (<= 1000 per page),
      page_token
    200 body: {"snapshots": {"AAPL240621C00190000": {
                   "latestQuote": {"t", "bp", "bs", "ap", "as", "bx", "ax", "c"},
                   "latestTrade": {"t", "p", "s", "x", "c"},
                   "impliedVolatility": 0.25,
                   "greeks": {"delta", "gamma", "theta", "vega", "rho"}}, ...},
               "next_page_token": "..." | null}

    GET {base_url}/v1beta1/news
      symbols, start, end (RFC 3339), sort (asc|desc), limit (<= 50 per page),
      include_content, exclude_contentless, page_token
    200 body: {"news": [{"id", "headline", "author", "source", "url", "summary", "content",
               "symbols", "created_at", "updated_at", "images"}], "next_page_token": ... }
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, ValidationError

from packages.market_data.errors import (
    MarketDataError,
    ProviderAccessError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from packages.market_data.models import (
    Adjustment,
    Bar,
    BarSeries,
    BarsRequest,
    DataFeed,
    Timeframe,
    TimeframeUnit,
)
from packages.market_data.news import NewsArticle, NewsBatch, NewsRequest
from packages.market_data.options import (
    OPTION_FEED_NOTICES,
    InvalidContractSymbolError,
    OptionChainRequest,
    OptionChainSnapshot,
    OptionContract,
    OptionFeed,
    OptionQuote,
    SkippedContract,
)
from packages.market_data.provider import MarketDataProvider
from packages.market_data.validation import normalize_bars
from packages.shared.timeutils import utc_now

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://data.alpaca.markets"
BARS_PATH = "/v2/stocks/bars"
NEWS_PATH = "/v1beta1/news"
NEWS_PAGE_SIZE_LIMIT = 50
OPTION_SNAPSHOTS_PATH = "/v1beta1/options/snapshots/{underlying}"
PAGE_SIZE_LIMIT = 10_000
OPTION_PAGE_SIZE_LIMIT = 1_000
_RETRYABLE_SERVER_STATUSES = frozenset({500, 502, 503, 504})
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_MAX_SECONDS = 8.0
_RETRY_AFTER_MAX_SECONDS = 30.0
_MAX_ERROR_DETAIL_CHARS = 200

# Allowed amounts per unit, mirroring the SDK's TimeFrame validation.
_SUPPORTED_TIMEFRAME_AMOUNTS: dict[TimeframeUnit, range | tuple[int, ...]] = {
    TimeframeUnit.MINUTE: range(1, 60),
    TimeframeUnit.HOUR: range(1, 24),
    TimeframeUnit.DAY: (1,),
    TimeframeUnit.WEEK: (1,),
    TimeframeUnit.MONTH: (1, 2, 3, 6, 12),
}

_FEED_NOTICES = {
    DataFeed.IEX: (
        "IEX feed: trades from the IEX exchange only, not the consolidated tape. "
        "Volume and some prices will differ from consolidated (SIP) data."
    ),
    DataFeed.SIP: (
        "SIP feed: consolidated tape from all U.S. exchanges. Recent SIP data may "
        "require a paid Alpaca market data subscription."
    ),
    DataFeed.DELAYED_SIP: "Delayed SIP feed: consolidated tape delayed by 15 minutes.",
}
_ADJUSTMENT_NOTICES = {
    Adjustment.RAW: "Prices are not adjusted for splits or dividends.",
    Adjustment.SPLIT: "Prices are adjusted for splits only.",
    Adjustment.DIVIDEND: "Prices are adjusted for dividends only.",
    Adjustment.ALL: "Prices are adjusted for splits and dividends.",
}
_EMPTY_NOTICE = (
    "The provider returned no bars for this range. Possible reasons: no trading "
    "in the range, the symbol is not available on this feed, or the symbol does not exist."
)
_INCOMPLETE_NOTICE = "The last bar's interval had not ended at retrieval time; it may still change."

Sleep = Callable[[float], Awaitable[None]]


class _AlpacaBar(BaseModel):
    """Alpaca's wire format for one bar. Unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")

    t: AwareDatetime
    o: Decimal
    h: Decimal
    low: Decimal = Field(alias="l")
    c: Decimal
    v: Decimal
    n: int | None = None
    vw: Decimal | None = None

    def to_bar(self) -> Bar:
        return Bar(
            timestamp=self.t,
            open=self.o,
            high=self.h,
            low=self.low,
            close=self.c,
            volume=self.v,
            trade_count=self.n,
            vwap=self.vw,
        )


class AlpacaMarketDataProvider(MarketDataProvider):
    name = "alpaca"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        default_feed: DataFeed = DataFeed.IEX,
        max_retries: int = 3,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Prefer `create()`. `client` must already carry base URL and auth headers."""
        self._client = client
        self._default_feed = default_feed
        self._max_retries = max_retries
        self._sleep = sleep

    @classmethod
    def create(
        cls,
        *,
        api_key: SecretStr,
        secret_key: SecretStr,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 10.0,
        default_feed: DataFeed = DataFeed.IEX,
        max_retries: int = 3,
        sleep: Sleep = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> Self:
        """Build a provider with its own HTTP client. `transport` exists for tests."""
        client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "APCA-API-KEY-ID": api_key.get_secret_value(),
                "APCA-API-SECRET-KEY": secret_key.get_secret_value(),
                "Accept": "application/json",
            },
            transport=transport,
        )
        return cls(client=client, default_feed=default_feed, max_retries=max_retries, sleep=sleep)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_bars(self, request: BarsRequest) -> BarSeries:
        timeframe = request.parsed_timeframe
        _check_supported_timeframe(timeframe)
        feed = request.feed or self._default_feed
        retrieved_at = utc_now()
        base_params: dict[str, Any] = {
            "symbols": request.symbol,
            "timeframe": str(timeframe),
            "start": _rfc3339(request.start),
            "end": _rfc3339(request.end),
            "adjustment": request.adjustment.value,
            "feed": feed.value,
            "sort": "asc",
        }

        collected: list[Bar] = []
        seen_tokens: set[str] = set()
        page_token: str | None = None
        truncated = False
        while True:
            params = {
                **base_params,
                "limit": min(PAGE_SIZE_LIMIT, request.max_bars - len(collected)),
            }
            if page_token is not None:
                params["page_token"] = page_token
            payload = await self._get_json(BARS_PATH, params)
            page_bars, page_token = _parse_bars_page(payload, request.symbol)
            collected.extend(page_bars)
            if page_token is None:
                break
            if len(collected) >= request.max_bars:
                truncated = True
                break
            if page_token in seen_tokens:
                raise ProviderResponseError(
                    "Alpaca repeated a pagination token; stopping to avoid an infinite loop",
                    provider=self.name,
                )
            seen_tokens.add(page_token)

        bars, duplicates = normalize_bars(
            collected, start=request.start, end=request.end, provider=self.name
        )
        last_bar_may_be_incomplete = bool(bars) and (
            bars[-1].timestamp + timeframe.approx_duration > retrieved_at
        )
        notices = [_FEED_NOTICES[feed], _ADJUSTMENT_NOTICES[request.adjustment]]
        if not bars:
            notices.append(_EMPTY_NOTICE)
        if truncated:
            notices.append(f"Result truncated at max_bars={request.max_bars}; more bars exist.")
        if duplicates:
            notices.append(f"{duplicates} exact duplicate bar(s) were removed.")
        if last_bar_may_be_incomplete:
            notices.append(_INCOMPLETE_NOTICE)

        logger.info(
            "bars retrieved",
            extra={
                "provider": self.name,
                "symbol": request.symbol,
                "timeframe": str(timeframe),
                "feed": feed.value,
                "bar_count": len(bars),
                "truncated": truncated,
                "duplicates_removed": duplicates,
            },
        )
        return BarSeries(
            symbol=request.symbol,
            timeframe=str(timeframe),
            provider=self.name,
            feed=feed,
            adjustment=request.adjustment,
            start=request.start,
            end=request.end,
            retrieved_at=retrieved_at,
            bars=bars,
            truncated=truncated,
            duplicates_removed=duplicates,
            last_bar_may_be_incomplete=last_bar_may_be_incomplete,
            notices=notices,
        )

    async def get_option_chain(self, request: OptionChainRequest) -> OptionChainSnapshot:
        feed = request.feed or OptionFeed.INDICATIVE
        retrieved_at = utc_now()
        base_params: dict[str, Any] = {"feed": feed.value}
        if request.option_type is not None:
            base_params["type"] = request.option_type.value
        for name in (
            "expiration_date_gte",
            "expiration_date_lte",
            "strike_price_gte",
            "strike_price_lte",
        ):
            value = getattr(request, name)
            if value is not None:
                base_params[name] = str(value)
        path = OPTION_SNAPSHOTS_PATH.format(underlying=request.underlying)

        quotes: list[OptionQuote] = []
        skipped: list[SkippedContract] = []
        seen_tokens: set[str] = set()
        page_token: str | None = None
        truncated = False
        while True:
            remaining = request.max_contracts - len(quotes) - len(skipped)
            params = {**base_params, "limit": min(OPTION_PAGE_SIZE_LIMIT, remaining)}
            if page_token is not None:
                params["page_token"] = page_token
            payload = await self._get_json(path, params)
            page_quotes, page_skipped, page_token = _parse_snapshots_page(
                payload, request.underlying
            )
            quotes.extend(page_quotes)
            skipped.extend(page_skipped)
            if page_token is None:
                break
            if len(quotes) + len(skipped) >= request.max_contracts:
                truncated = True
                break
            if page_token in seen_tokens:
                raise ProviderResponseError(
                    "Alpaca repeated a pagination token; stopping to avoid an infinite loop",
                    provider=self.name,
                )
            seen_tokens.add(page_token)

        quotes.sort(key=lambda q: q.symbol)
        notices = [OPTION_FEED_NOTICES[feed]]
        if not quotes:
            notices.append(
                "The provider returned no usable option quotes. Possible reasons: no listed "
                "options match the filters, the feed is not covered by your subscription, "
                "or the underlying has no options."
            )
        if skipped:
            notices.append(f"{len(skipped)} contract(s) skipped; see `skipped` for reasons.")
        if truncated:
            notices.append(
                f"Result truncated at max_contracts={request.max_contracts}; more contracts exist."
            )
        logger.info(
            "option chain retrieved",
            extra={
                "provider": self.name,
                "underlying": request.underlying,
                "feed": feed.value,
                "quote_count": len(quotes),
                "skipped_count": len(skipped),
                "truncated": truncated,
            },
        )
        return OptionChainSnapshot(
            underlying=request.underlying,
            provider=self.name,
            feed=feed,
            retrieved_at=retrieved_at,
            quotes=quotes,
            skipped=skipped,
            truncated=truncated,
            notices=notices,
        )

    async def get_news(self, request: NewsRequest) -> NewsBatch:
        retrieved_at = utc_now()
        base = {
            "symbols": request.symbol,
            "start": _rfc3339(request.start),
            "end": _rfc3339(request.end),
            "sort": "desc",
            "include_content": "true",
        }
        articles: list[NewsArticle] = []
        seen_tokens: set[str] = set()
        page_token: str | None = None
        truncated = False
        while True:
            params = {
                **base,
                "limit": min(NEWS_PAGE_SIZE_LIMIT, request.max_articles - len(articles)),
            }
            if page_token is not None:
                params["page_token"] = page_token
            payload = await self._get_json(NEWS_PATH, params)
            page, page_token = _parse_news_page(payload)
            articles.extend(page)
            if page_token is None:
                break
            if len(articles) >= request.max_articles:
                truncated = True
                break
            if page_token in seen_tokens:
                raise ProviderResponseError(
                    "Alpaca repeated a pagination token; stopping to avoid an infinite loop",
                    provider=self.name,
                )
            seen_tokens.add(page_token)
        notices = [
            "News text is third-party content: analyse it as data, verify material claims "
            "against primary sources, and respect the provider's licence terms."
        ]
        if not articles:
            notices.append("The provider returned no articles for this symbol and range.")
        if truncated:
            notices.append(f"Truncated at max_articles={request.max_articles}.")
        logger.info(
            "news retrieved",
            extra={"provider": self.name, "symbol": request.symbol, "article_count": len(articles)},
        )
        return NewsBatch(
            symbol=request.symbol,
            provider=self.name,
            start=request.start,
            end=request.end,
            retrieved_at=retrieved_at,
            articles=articles,
            truncated=truncated,
            notices=notices,
        )

    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET one page, retrying transient failures with bounded exponential backoff."""
        max_attempts = self._max_retries + 1
        for attempt in range(1, max_attempts + 1):
            error: MarketDataError
            try:
                response = await self._client.get(path, params=params)
            except httpx.TimeoutException:
                error = ProviderUnavailableError("Alpaca request timed out", provider=self.name)
            except httpx.TransportError as exc:
                error = ProviderUnavailableError(
                    f"network error contacting Alpaca ({type(exc).__name__})", provider=self.name
                )
            else:
                if response.status_code == 200:
                    return _decode_json(response)
                error = _error_from_response(response)

            if not error.retryable or attempt == max_attempts:
                if error.retryable:
                    logger.error(
                        "market data request failed after retries",
                        extra={
                            "provider": self.name,
                            "attempts": attempt,
                            "reason": error.error_type,
                        },
                    )
                raise error
            delay = _backoff_delay(attempt, error.retry_after)
            logger.warning(
                "retrying market data request",
                extra={
                    "provider": self.name,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "delay_seconds": delay,
                    "reason": error.error_type,
                },
            )
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover


def _check_supported_timeframe(timeframe: Timeframe) -> None:
    if timeframe.amount not in _SUPPORTED_TIMEFRAME_AMOUNTS[timeframe.unit]:
        raise ProviderRequestError(
            f"Alpaca does not support timeframe {timeframe}. Supported: 1-59Min, 1-23Hour, "
            "1Day, 1Week, 1/2/3/6/12Month",
            provider=AlpacaMarketDataProvider.name,
        )


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, _RETRY_AFTER_MAX_SECONDS)
    return min(_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), _BACKOFF_MAX_SECONDS)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None  # HTTP-date form is not worth supporting here; fall back to backoff
    return seconds if seconds >= 0 else None


def _provider_detail(response: httpx.Response) -> str:
    """Short, human-readable error text from the provider's response body."""
    try:
        body = response.json()
        detail = body.get("message") if isinstance(body, dict) else None
    except ValueError:
        detail = None
    text = str(detail) if detail else response.text
    return text.strip()[:_MAX_ERROR_DETAIL_CHARS] or "no details provided"


def _error_from_response(response: httpx.Response) -> MarketDataError:
    status = response.status_code
    provider = AlpacaMarketDataProvider.name
    if status in (401, 403):
        return ProviderAccessError(
            f"Alpaca refused access (HTTP {status}: {_provider_detail(response)}). Check that "
            "ALPACA_API_KEY/ALPACA_SECRET_KEY are valid and that your market data "
            "subscription covers the requested feed and date range.",
            provider=provider,
        )
    if status == 429:
        return ProviderRateLimitError(
            "Alpaca rate limit exceeded",
            provider=provider,
            retry_after=_retry_after_seconds(response),
        )
    if status in _RETRYABLE_SERVER_STATUSES:
        return ProviderUnavailableError(
            f"Alpaca returned HTTP {status}",
            provider=provider,
            retry_after=_retry_after_seconds(response),
        )
    if 400 <= status < 500:
        return ProviderRequestError(
            f"Alpaca rejected the request (HTTP {status}: {_provider_detail(response)})",
            provider=provider,
        )
    return ProviderResponseError(f"unexpected HTTP {status} from Alpaca", provider=provider)


def _decode_json(response: httpx.Response) -> dict[str, Any]:
    # parse_float=Decimal keeps prices exact instead of round-tripping through float.
    try:
        payload = json.loads(response.text, parse_float=Decimal)
    except json.JSONDecodeError:
        raise ProviderResponseError(
            "Alpaca returned a response that is not valid JSON",
            provider=AlpacaMarketDataProvider.name,
        ) from None
    if not isinstance(payload, dict):
        raise ProviderResponseError(
            "Alpaca returned JSON that is not an object", provider=AlpacaMarketDataProvider.name
        )
    return payload


def _parse_bars_page(payload: dict[str, Any], symbol: str) -> tuple[list[Bar], str | None]:
    provider = AlpacaMarketDataProvider.name
    if "bars" not in payload:
        raise ProviderResponseError(
            "Alpaca response is missing the 'bars' field", provider=provider
        )
    bars_by_symbol = {} if payload["bars"] is None else payload["bars"]
    if not isinstance(bars_by_symbol, dict):
        raise ProviderResponseError(
            "Alpaca 'bars' field is not an object keyed by symbol", provider=provider
        )
    raw_bars = bars_by_symbol.get(symbol) or []
    if not isinstance(raw_bars, list):
        raise ProviderResponseError(f"Alpaca bars for {symbol} are not a list", provider=provider)

    token = payload.get("next_page_token")
    if token is not None and (not isinstance(token, str) or not token):
        raise ProviderResponseError("Alpaca returned an invalid next_page_token", provider=provider)

    bars: list[Bar] = []
    for index, raw in enumerate(raw_bars):
        try:
            bars.append(_AlpacaBar.model_validate(raw).to_bar())
        except ValidationError as exc:
            raise ProviderResponseError(
                f"Alpaca returned an invalid bar at index {index}: {_summarize(exc)}",
                provider=provider,
            ) from exc
    return bars, token


def _summarize(exc: ValidationError) -> str:
    parts = [
        f"{'.'.join(str(p) for p in err['loc']) or 'bar'}: {err['msg']}"
        for err in exc.errors(include_url=False)[:3]
    ]
    return "; ".join(parts)


class _AlpacaOptionQuote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    t: AwareDatetime
    bp: Decimal
    ap: Decimal
    bs: Decimal
    as_: Decimal = Field(alias="as")
    bx: str | None = None
    ax: str | None = None
    c: str | None = None


class _AlpacaOptionTrade(BaseModel):
    model_config = ConfigDict(extra="ignore")

    t: AwareDatetime
    p: Decimal


class _AlpacaGreeks(BaseModel):
    model_config = ConfigDict(extra="ignore")

    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    rho: Decimal | None = None


class _AlpacaOptionSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    latest_quote: _AlpacaOptionQuote | None = Field(default=None, alias="latestQuote")
    latest_trade: _AlpacaOptionTrade | None = Field(default=None, alias="latestTrade")
    implied_volatility: Decimal | None = Field(default=None, alias="impliedVolatility")
    greeks: _AlpacaGreeks | None = None


def _parse_snapshots_page(
    payload: dict[str, Any], underlying: str
) -> tuple[list[OptionQuote], list[SkippedContract], str | None]:
    provider = AlpacaMarketDataProvider.name
    if "snapshots" not in payload:
        raise ProviderResponseError(
            "Alpaca response is missing the 'snapshots' field", provider=provider
        )
    snapshots = {} if payload["snapshots"] is None else payload["snapshots"]
    if not isinstance(snapshots, dict):
        raise ProviderResponseError(
            "Alpaca 'snapshots' field is not an object keyed by contract", provider=provider
        )
    token = payload.get("next_page_token")
    if token is not None and (not isinstance(token, str) or not token):
        raise ProviderResponseError("Alpaca returned an invalid next_page_token", provider=provider)

    quotes: list[OptionQuote] = []
    skipped: list[SkippedContract] = []
    for symbol, raw in snapshots.items():
        try:
            contract = OptionContract.parse(str(symbol))
        except InvalidContractSymbolError as exc:
            raise ProviderResponseError(
                f"Alpaca returned an unrecognised contract symbol: {exc}", provider=provider
            ) from None
        if contract.root != underlying:
            skipped.append(
                SkippedContract(
                    symbol=contract.symbol,
                    reason=f"non-standard root {contract.root} (adjusted, index or renamed "
                    "contract); only 100-share standard contracts are supported",
                )
            )
            continue
        try:
            snapshot = _AlpacaOptionSnapshot.model_validate(raw)
        except ValidationError as exc:
            raise ProviderResponseError(
                f"Alpaca returned an invalid snapshot for {contract.symbol}: {_summarize(exc)}",
                provider=provider,
            ) from exc
        quote = snapshot.latest_quote
        if quote is None:
            skipped.append(SkippedContract(symbol=contract.symbol, reason="no latest quote"))
            continue
        greeks = snapshot.greeks or _AlpacaGreeks()
        try:
            quotes.append(
                OptionQuote(
                    symbol=contract.symbol,
                    quoted_at=quote.t,
                    bid=quote.bp,
                    ask=quote.ap,
                    bid_size=quote.bs,
                    ask_size=quote.as_,
                    bid_exchange=quote.bx,
                    ask_exchange=quote.ax,
                    condition=quote.c,
                    last_trade_price=snapshot.latest_trade.p if snapshot.latest_trade else None,
                    last_trade_at=snapshot.latest_trade.t if snapshot.latest_trade else None,
                    implied_volatility=snapshot.implied_volatility,
                    delta=greeks.delta,
                    gamma=greeks.gamma,
                    theta=greeks.theta,
                    vega=greeks.vega,
                    rho=greeks.rho,
                )
            )
        except ValidationError as exc:
            # A crossed or negative quote is the provider's data, not ours to repair.
            skipped.append(
                SkippedContract(symbol=contract.symbol, reason=f"unusable quote: {_summarize(exc)}")
            )
    return quotes, skipped, token


class _AlpacaNews(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | str
    headline: str
    author: str | None = None
    source: str | None = None
    url: str | None = None
    summary: str | None = None
    content: str | None = None
    symbols: list[str] = Field(default_factory=list)
    created_at: AwareDatetime
    updated_at: AwareDatetime


def _parse_news_page(payload: dict[str, Any]) -> tuple[list[NewsArticle], str | None]:
    provider = AlpacaMarketDataProvider.name
    if "news" not in payload:
        raise ProviderResponseError(
            "Alpaca response is missing the 'news' field", provider=provider
        )
    rows = [] if payload["news"] is None else payload["news"]
    if not isinstance(rows, list):
        raise ProviderResponseError("Alpaca 'news' field is not a list", provider=provider)
    token = payload.get("next_page_token")
    if token is not None and (not isinstance(token, str) or not token):
        raise ProviderResponseError("Alpaca returned an invalid next_page_token", provider=provider)
    articles = []
    for index, raw in enumerate(rows):
        try:
            item = _AlpacaNews.model_validate(raw)
        except ValidationError as exc:
            raise ProviderResponseError(
                f"Alpaca returned an invalid news item at index {index}: {_summarize(exc)}",
                provider=provider,
            ) from exc
        articles.append(
            NewsArticle(
                provider=provider,
                provider_id=str(item.id),
                headline=item.headline,
                source=item.source,
                author=item.author,
                url=item.url or None,
                summary=item.summary or "",
                content=item.content or "",
                symbols=item.symbols,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
        )
    return articles, token
