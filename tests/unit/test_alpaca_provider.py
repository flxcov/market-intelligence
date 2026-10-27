"""Alpaca adapter tests against a mocked HTTP transport (no network, synthetic data)."""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from packages.market_data.alpaca import BARS_PATH, AlpacaMarketDataProvider
from packages.market_data.errors import (
    DataValidationError,
    ProviderAccessError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from packages.market_data.models import Adjustment, BarsRequest, DataFeed
from tests.fixtures.alpaca_payloads import SYNTHETIC_DAILY_BARS, bars_page, synthetic_bar

pytestmark = pytest.mark.anyio

API_KEY = "test-key-id-AAAA1111"
SECRET_KEY = "test-secret-BBBB2222"

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """Serves queued responses (or raises queued exceptions) and records requests."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def ok(text: str) -> httpx.Response:
    return httpx.Response(200, text=text, headers={"Content-Type": "application/json"})


def make_provider(
    recorder: Recorder, *, max_retries: int = 3, sleep: SleepRecorder | None = None
) -> AlpacaMarketDataProvider:
    return AlpacaMarketDataProvider.create(
        api_key=SecretStr(API_KEY),
        secret_key=SecretStr(SECRET_KEY),
        max_retries=max_retries,
        sleep=sleep or SleepRecorder(),
        transport=httpx.MockTransport(recorder),
    )


def make_request(**overrides: object) -> BarsRequest:
    fields: dict[str, object] = {"symbol": "AAPL", "start": "2024-01-01", "end": "2024-01-31"}
    fields.update(overrides)
    return BarsRequest(**fields)


# --- success paths -------------------------------------------------------------


async def test_parses_bars_with_exact_decimals() -> None:
    recorder = Recorder(ok(bars_page("AAPL", SYNTHETIC_DAILY_BARS)))
    series = await make_provider(recorder).get_bars(make_request())

    assert series.bar_count == 3
    first = series.bars[0]
    assert first.timestamp == datetime(2024, 1, 2, 5, tzinfo=UTC)
    assert first.open == Decimal("100.0")
    assert first.low == Decimal("99.25")
    assert first.vwap == Decimal("100.4")
    assert first.trade_count == 10
    assert series.provider == "alpaca"
    assert series.feed is DataFeed.IEX
    assert series.truncated is False
    assert series.last_bar_may_be_incomplete is False
    assert any("IEX" in notice for notice in series.notices)


async def test_sends_expected_request() -> None:
    recorder = Recorder(ok(bars_page("AAPL", [])))
    request = make_request(
        timeframe="15Min",
        start="2024-01-02T14:30:00Z",
        end="2024-01-02T21:00:00Z",
        adjustment="split",
        feed="delayed_sip",
    )
    await make_provider(recorder).get_bars(request)

    sent = recorder.requests[0]
    assert sent.url.host == "data.alpaca.markets"
    assert sent.url.path == BARS_PATH
    assert dict(sent.url.params) == {
        "symbols": "AAPL",
        "timeframe": "15Min",
        "start": "2024-01-02T14:30:00Z",
        "end": "2024-01-02T21:00:00Z",
        "adjustment": "split",
        "feed": "delayed_sip",
        "sort": "asc",
        "limit": "10000",
    }
    assert sent.headers["APCA-API-KEY-ID"] == API_KEY
    assert sent.headers["APCA-API-SECRET-KEY"] == SECRET_KEY


async def test_follows_pagination() -> None:
    recorder = Recorder(
        ok(bars_page("AAPL", SYNTHETIC_DAILY_BARS[:2], next_page_token="page-2")),
        ok(bars_page("AAPL", SYNTHETIC_DAILY_BARS[2:])),
    )
    series = await make_provider(recorder).get_bars(make_request())

    assert series.bar_count == 3
    assert "page_token" not in recorder.requests[0].url.params
    assert recorder.requests[1].url.params["page_token"] == "page-2"


async def test_stops_at_max_bars_and_flags_truncation() -> None:
    recorder = Recorder(ok(bars_page("AAPL", SYNTHETIC_DAILY_BARS[:2], next_page_token="more")))
    series = await make_provider(recorder).get_bars(make_request(max_bars=2))

    assert series.bar_count == 2
    assert series.truncated is True
    assert recorder.requests[0].url.params["limit"] == "2"
    assert len(recorder.requests) == 1
    assert any("truncated" in notice for notice in series.notices)


@pytest.mark.parametrize("body", [{"bars": {}}, {"bars": None}, {"bars": {"AAPL": None}}])
async def test_empty_results_are_returned_as_empty_not_invented(body: dict[str, object]) -> None:
    recorder = Recorder(ok(json.dumps({**body, "next_page_token": None})))
    series = await make_provider(recorder).get_bars(make_request())

    assert series.bars == []
    assert any("no bars" in notice for notice in series.notices)


async def test_exact_duplicate_bars_are_removed() -> None:
    bar = synthetic_bar()
    recorder = Recorder(ok(bars_page("AAPL", [bar, bar])))
    series = await make_provider(recorder).get_bars(make_request())

    assert series.bar_count == 1
    assert series.duplicates_removed == 1


async def test_recent_bar_is_flagged_as_possibly_incomplete() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    recent = synthetic_bar(t=(now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    recorder = Recorder(ok(bars_page("AAPL", [recent])))
    request = make_request(
        timeframe="15Min", start=(now - timedelta(hours=1)).isoformat(), end=now.isoformat()
    )
    series = await make_provider(recorder).get_bars(request)

    assert series.last_bar_may_be_incomplete is True


async def test_adjustment_is_reported() -> None:
    recorder = Recorder(ok(bars_page("AAPL", [])))
    series = await make_provider(recorder).get_bars(make_request(adjustment="all"))
    assert series.adjustment is Adjustment.ALL
    assert any("splits and dividends" in notice for notice in series.notices)


# --- request validation --------------------------------------------------------


@pytest.mark.parametrize("timeframe", ["60Min", "24Hour", "2Day", "2Week", "4Month"])
async def test_unsupported_timeframe_fails_without_network_call(timeframe: str) -> None:
    recorder = Recorder()
    with pytest.raises(ProviderRequestError, match="does not support timeframe"):
        await make_provider(recorder).get_bars(make_request(timeframe=timeframe))
    assert recorder.requests == []


# --- error handling ------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors_are_not_retried(status: int) -> None:
    recorder = Recorder(httpx.Response(status, json={"message": "forbidden."}))
    with pytest.raises(ProviderAccessError, match="forbidden") as exc_info:
        await make_provider(recorder).get_bars(make_request())
    assert len(recorder.requests) == 1
    assert API_KEY not in exc_info.value.message
    assert SECRET_KEY not in exc_info.value.message


async def test_bad_request_is_reported_with_provider_detail() -> None:
    recorder = Recorder(httpx.Response(422, json={"message": "invalid symbol"}))
    with pytest.raises(ProviderRequestError, match="invalid symbol"):
        await make_provider(recorder).get_bars(make_request())
    assert len(recorder.requests) == 1


async def test_rate_limit_is_retried_honoring_retry_after() -> None:
    sleep = SleepRecorder()
    recorder = Recorder(
        httpx.Response(429, headers={"Retry-After": "2"}),
        ok(bars_page("AAPL", SYNTHETIC_DAILY_BARS)),
    )
    series = await make_provider(recorder, sleep=sleep).get_bars(make_request())

    assert series.bar_count == 3
    assert sleep.delays == [2.0]


async def test_rate_limit_exhaustion_raises() -> None:
    sleep = SleepRecorder()
    recorder = Recorder(*[httpx.Response(429) for _ in range(3)])
    with pytest.raises(ProviderRateLimitError):
        await make_provider(recorder, max_retries=2, sleep=sleep).get_bars(make_request())
    assert len(recorder.requests) == 3
    assert sleep.delays == [0.5, 1.0]  # exponential backoff


async def test_server_errors_retry_then_give_up() -> None:
    recorder = Recorder(*[httpx.Response(503) for _ in range(4)])
    with pytest.raises(ProviderUnavailableError, match="503"):
        await make_provider(recorder, max_retries=3).get_bars(make_request())
    assert len(recorder.requests) == 4


async def test_timeouts_are_retried() -> None:
    recorder = Recorder(
        httpx.ReadTimeout("timed out"),
        ok(bars_page("AAPL", SYNTHETIC_DAILY_BARS)),
    )
    series = await make_provider(recorder).get_bars(make_request())
    assert series.bar_count == 3


async def test_network_failure_raises_unavailable() -> None:
    recorder = Recorder(httpx.ConnectError("refused"), httpx.ConnectError("refused"))
    with pytest.raises(ProviderUnavailableError, match="ConnectError"):
        await make_provider(recorder, max_retries=1).get_bars(make_request())


async def test_retry_after_is_capped() -> None:
    sleep = SleepRecorder()
    recorder = Recorder(
        httpx.Response(429, headers={"Retry-After": "3600"}), ok(bars_page("AAPL", []))
    )
    await make_provider(recorder, sleep=sleep).get_bars(make_request())
    assert sleep.delays == [30.0]


# --- malformed responses -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("<html>oops</html>", "not valid JSON"),
        ("[]", "not an object"),
        ('{"next_page_token": null}', "missing the 'bars' field"),
        ('{"bars": [1, 2]}', "not an object keyed by symbol"),
        ('{"bars": {"AAPL": {"t": 1}}}', "not a list"),
        ('{"bars": {"AAPL": []}, "next_page_token": 5}', "invalid next_page_token"),
    ],
)
async def test_malformed_responses_raise(text: str, match: str) -> None:
    recorder = Recorder(ok(text))
    with pytest.raises(ProviderResponseError, match=match):
        await make_provider(recorder).get_bars(make_request())


@pytest.mark.parametrize(
    "bad_bar",
    [
        {"t": "2024-01-02T05:00:00Z", "o": 1, "h": 2, "l": 0.5},  # missing close/volume
        synthetic_bar(h=50.0),  # high below open
        synthetic_bar(o=-1.0),
        synthetic_bar(t="2024-01-02T05:00:00"),  # naive timestamp
        {**synthetic_bar(), "c": "abc"},
    ],
)
async def test_invalid_bars_are_rejected_not_repaired(bad_bar: dict[str, object]) -> None:
    recorder = Recorder(ok(bars_page("AAPL", [bad_bar])))
    with pytest.raises(ProviderResponseError, match="invalid bar at index 0"):
        await make_provider(recorder).get_bars(make_request())


async def test_conflicting_duplicates_raise() -> None:
    recorder = Recorder(ok(bars_page("AAPL", [synthetic_bar(c=100.75), synthetic_bar(c=100.8)])))
    with pytest.raises(DataValidationError, match="conflicting"):
        await make_provider(recorder).get_bars(make_request())


async def test_bars_outside_requested_range_raise() -> None:
    recorder = Recorder(ok(bars_page("AAPL", [synthetic_bar(t="2023-12-29T05:00:00Z")])))
    with pytest.raises(DataValidationError, match="outside the requested range"):
        await make_provider(recorder).get_bars(make_request())


async def test_repeated_page_token_aborts() -> None:
    recorder = Recorder(
        ok(bars_page("AAPL", [synthetic_bar(t="2024-01-02T05:00:00Z")], next_page_token="x")),
        ok(bars_page("AAPL", [synthetic_bar(t="2024-01-03T05:00:00Z")], next_page_token="x")),
    )
    with pytest.raises(ProviderResponseError, match="repeated a pagination token"):
        await make_provider(recorder).get_bars(make_request())


# --- secrets -------------------------------------------------------------------


async def test_secrets_never_appear_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = Recorder(httpx.Response(503), httpx.Response(401, json={"message": "unauthorized"}))
    with pytest.raises(ProviderAccessError):
        await make_provider(recorder).get_bars(make_request())

    logged = "\n".join(f"{r.getMessage()} {vars(r)}" for r in caplog.records)
    assert caplog.records  # the retry was logged
    assert API_KEY not in logged
    assert SECRET_KEY not in logged
