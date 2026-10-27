"""SEC EDGAR client and parsers against a mocked transport (synthetic payloads)."""

import json
import logging
from decimal import Decimal

import httpx
import pytest

from packages.sec.client import COMPANY_FACTS_URL, SUBMISSIONS_URL, SecEdgarClient
from packages.sec.errors import (
    SecAccessError,
    SecConfigurationError,
    SecNotFoundError,
    SecResponseError,
    SecUnavailableError,
)
from packages.sec.parsing import parse_facts, parse_submissions, parse_ticker_map
from tests.fixtures import sec_payloads as sp

pytestmark = pytest.mark.anyio
UA = "Synthetic Tests tester@example.com"


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Sleeps:
    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, delay):
        self.delays.append(delay)


def client(recorder, sleep=None, clock=None, max_retries=2) -> SecEdgarClient:
    sec = SecEdgarClient.create(
        user_agent=UA,
        max_retries=max_retries,
        sleep=sleep or Sleeps(),
        transport=httpx.MockTransport(recorder),
    )
    if clock is not None:
        sec._clock = clock
    return sec


def ok(payload) -> httpx.Response:
    return httpx.Response(200, text=json.dumps(payload))


async def test_requests_identify_the_caller_and_keep_decimals_exact() -> None:
    recorder = Recorder(httpx.Response(200, text='{"facts": {}, "x": 1.10}'))
    document = await client(recorder).company_facts(sp.CIK)
    [request] = recorder.requests
    assert str(request.url) == "https://data.sec.gov/api/xbrl/companyfacts/CIK0001234567.json"
    assert request.headers["User-Agent"] == UA
    assert document.payload["x"] == Decimal("1.10")
    assert len(document.sha256) == 64
    assert document.url == COMPANY_FACTS_URL.format(cik=sp.CIK)


def test_user_agent_must_carry_a_contact_email() -> None:
    with pytest.raises(SecConfigurationError, match="contact email"):
        SecEdgarClient.create(user_agent="no contact here")


async def test_requests_are_paced_under_the_sec_rate_limit() -> None:
    times = iter([0.0, 0.0, 0.05, 0.125])
    sleeps = Sleeps()
    recorder = Recorder(ok({}), ok({}))
    sec = client(recorder, sleep=sleeps, clock=lambda: next(times))
    await sec.get_json(SUBMISSIONS_URL.format(cik=1))
    await sec.get_json(SUBMISSIONS_URL.format(cik=2))
    assert sleeps.delays == [pytest.approx(0.075)]  # 0.125s spacing


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(404, text="<Error/>"), SecNotFoundError),
        (httpx.Response(403, text="denied"), SecAccessError),
        (httpx.Response(200, text="<html>"), SecResponseError),
        (httpx.Response(200, text="[1, 2]"), SecResponseError),
        (httpx.Response(418, text="?"), SecResponseError),
    ],
)
async def test_non_retryable_failures(response, error) -> None:
    recorder = Recorder(response)
    with pytest.raises(error) as raised:
        await client(recorder).submissions(1)
    assert len(recorder.requests) == 1
    assert "tester@example.com" not in raised.value.message


async def test_transient_failures_retry_with_bounded_backoff() -> None:
    sleeps = Sleeps()
    recorder = Recorder(httpx.Response(429), httpx.ConnectError("x"), httpx.Response(503))
    with pytest.raises(SecUnavailableError):
        await client(recorder, sleep=sleeps, max_retries=2).submissions(1)
    assert len(recorder.requests) == 3
    assert [d for d in sleeps.delays if d >= 1] == [1.0, 2.0]


async def test_contact_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    await client(Recorder(httpx.Response(503), ok({}))).submissions(1)
    assert "tester@example.com" not in caplog.text


def test_parse_ticker_map() -> None:
    entries = parse_ticker_map(sp.ticker_map())
    assert entries["SYNT"].cik == sp.CIK and entries["SYNT"].title == sp.NAME
    with pytest.raises(SecResponseError):
        parse_ticker_map({"0": {"ticker": "X"}})


def test_parse_submissions_builds_filing_urls() -> None:
    profile = parse_submissions(sp.submissions())
    assert (profile.cik, profile.exchanges, profile.sic_description) == (
        sp.CIK,
        ["Nasdaq"],
        "Services-Prepackaged Software",
    )
    eight_k = profile.filings[0]
    assert eight_k.form == "8-K" and eight_k.items == "2.02,9.01"
    assert eight_k.url == (
        "https://www.sec.gov/Archives/edgar/data/1234567/000123456724000011/synt-8k.htm"
    )
    broken = sp.submissions()
    broken["filings"]["recent"]["form"].pop()
    with pytest.raises(SecResponseError, match="different lengths"):
        parse_submissions(broken)


def test_parse_facts() -> None:
    facts = parse_facts(sp.company_facts(), "us-gaap", "Revenues")
    assert len(facts) == 6
    fy23 = next(f for f in facts if f.end.isoformat() == "2023-12-31" and f.duration_days == 364)
    assert fy23.value == Decimal(100_000_000) and fy23.concept == "us-gaap:Revenues"
    assert parse_facts(sp.company_facts(), "us-gaap", "NotThere") == []
    bad = sp.company_facts()
    bad["facts"]["us-gaap"]["Revenues"]["units"]["USD"][0]["end"] = "not a date"
    with pytest.raises(SecResponseError):
        parse_facts(bad, "us-gaap", "Revenues")
