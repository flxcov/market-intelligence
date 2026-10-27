"""Alpaca option chain adapter against a mocked HTTP transport (synthetic data, no network)."""

import logging
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from packages.market_data.errors import (
    ProviderAccessError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from packages.market_data.options import OptionChainRequest, OptionFeed, OptionType
from packages.market_data.provider import MarketDataProvider
from tests.fixtures.alpaca_option_payloads import snapshots_page, synthetic_snapshot
from tests.unit.test_alpaca_provider import (
    API_KEY,
    SECRET_KEY,
    Recorder,
    SleepRecorder,
    make_provider,
    ok,
)

pytestmark = pytest.mark.anyio

D = Decimal
CALL = "AAPL240621C00190000"
PUT = "AAPL240621P00185000"


def chain(**overrides: object) -> OptionChainRequest:
    fields: dict[str, object] = {"underlying": "AAPL", "feed": OptionFeed.INDICATIVE}
    fields.update(overrides)
    return OptionChainRequest(**fields)


async def test_parses_quotes_with_exact_decimals_and_greeks() -> None:
    recorder = Recorder(ok(snapshots_page({CALL: synthetic_snapshot()})))
    snapshot = await make_provider(recorder).get_option_chain(chain())
    assert snapshot.feed is OptionFeed.INDICATIVE
    assert snapshot.provider == "alpaca"
    [q] = snapshot.quotes
    assert q.symbol == CALL
    assert q.quoted_at == datetime(2024, 6, 3, 15, 0, tzinfo=UTC)
    assert (q.bid, q.ask, q.bid_size, q.ask_size) == (D("2.4"), D("2.5"), D(10), D(12))
    assert q.last_trade_price == D("2.45")
    assert q.implied_volatility == D("0.2512")
    assert q.delta == D("0.52") and q.theta == D("-0.05")
    assert any("indicative" in n.lower() for n in snapshot.notices)


async def test_sends_expected_request() -> None:
    recorder = Recorder(ok(snapshots_page({})))
    await make_provider(recorder).get_option_chain(
        chain(
            option_type=OptionType.CALL,
            expiration_date_gte=date(2024, 6, 1),
            expiration_date_lte=date(2024, 6, 30),
            strike_price_gte=D("180"),
            strike_price_lte=D("200.5"),
            max_contracts=50,
        )
    )
    [request] = recorder.requests
    assert request.url.path == "/v1beta1/options/snapshots/AAPL"
    params = dict(request.url.params)
    assert params == {
        "feed": "indicative",
        "type": "call",
        "expiration_date_gte": "2024-06-01",
        "expiration_date_lte": "2024-06-30",
        "strike_price_gte": "180",
        "strike_price_lte": "200.5",
        "limit": "50",
    }
    assert request.headers["APCA-API-KEY-ID"] == API_KEY


async def test_follows_pagination_and_truncates() -> None:
    recorder = Recorder(
        ok(snapshots_page({CALL: synthetic_snapshot()}, next_page_token="p2")),
        ok(snapshots_page({PUT: synthetic_snapshot()}, next_page_token="p3")),
    )
    snapshot = await make_provider(recorder).get_option_chain(chain(max_contracts=2))
    assert [q.symbol for q in snapshot.quotes] == [CALL, PUT]
    assert snapshot.truncated
    assert recorder.requests[1].url.params["page_token"] == "p2"
    assert recorder.requests[1].url.params["limit"] == "1"


async def test_contracts_without_usable_quotes_are_skipped_not_invented() -> None:
    recorder = Recorder(
        ok(
            snapshots_page(
                {
                    CALL: synthetic_snapshot(quote=False),
                    PUT: synthetic_snapshot(bp=3.0, ap=2.0),  # crossed
                    "AAPL1240621C00190000": synthetic_snapshot(),  # adjusted contract
                    "AAPL240621C00195000": synthetic_snapshot(trade=False, greeks=False),
                }
            )
        )
    )
    snapshot = await make_provider(recorder).get_option_chain(chain())
    assert [q.symbol for q in snapshot.quotes] == ["AAPL240621C00195000"]
    assert snapshot.quotes[0].last_trade_price is None
    assert snapshot.quotes[0].delta is None
    reasons = {s.symbol: s.reason for s in snapshot.skipped}
    assert reasons[CALL] == "no latest quote"
    assert "crossed" in reasons[PUT]
    assert "non-standard root" in reasons["AAPL1240621C00190000"]


async def test_empty_chain_is_reported_not_invented() -> None:
    snapshot = await make_provider(Recorder(ok(snapshots_page({})))).get_option_chain(chain())
    assert snapshot.quotes == []
    assert any("no usable option quotes" in n for n in snapshot.notices)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ('{"no_snapshots": {}}', "missing the 'snapshots'"),
        ('{"snapshots": []}', "not an object"),
        ('{"snapshots": {"NOTANOPTION": {}}}', "unrecognised contract"),
        ('{"snapshots": {}, "next_page_token": 5}', "next_page_token"),
        (
            '{"snapshots": {"AAPL240621C00190000": {"latestQuote": {"t": "bad"}}}}',
            "invalid snapshot",
        ),
        ("not json", "not valid JSON"),
    ],
)
async def test_malformed_responses_raise(text: str, match: str) -> None:
    with pytest.raises(ProviderResponseError, match=match):
        await make_provider(Recorder(ok(text))).get_option_chain(chain())


async def test_subscription_errors_are_not_retried() -> None:
    recorder = Recorder(httpx.Response(403, json={"message": "subscription does not permit"}))
    with pytest.raises(ProviderAccessError, match="subscription"):
        await make_provider(recorder).get_option_chain(chain(feed=OptionFeed.OPRA))
    assert len(recorder.requests) == 1


async def test_server_errors_are_retried_with_bounded_backoff() -> None:
    sleep = SleepRecorder()
    recorder = Recorder(httpx.Response(503), httpx.Response(503))
    with pytest.raises(ProviderUnavailableError):
        await make_provider(recorder, max_retries=1, sleep=sleep).get_option_chain(chain())
    assert sleep.delays == [0.5]


async def test_repeated_page_token_aborts() -> None:
    recorder = Recorder(
        ok(snapshots_page({CALL: synthetic_snapshot()}, next_page_token="same")),
        ok(snapshots_page({PUT: synthetic_snapshot()}, next_page_token="same")),
    )
    with pytest.raises(ProviderResponseError, match="repeated a pagination token"):
        await make_provider(recorder).get_option_chain(chain())


async def test_providers_without_options_refuse() -> None:
    class BarsOnly(MarketDataProvider):
        name = "bars-only"

        async def get_bars(self, request):  # pragma: no cover - not called
            raise NotImplementedError

    with pytest.raises(ProviderRequestError, match="does not supply option quotes"):
        await BarsOnly().get_option_chain(chain())


async def test_secrets_never_appear_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = Recorder(ok(snapshots_page({CALL: synthetic_snapshot()})))
    await make_provider(recorder).get_option_chain(chain())
    assert API_KEY not in caplog.text and SECRET_KEY not in caplog.text
