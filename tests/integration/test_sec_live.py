"""Live checks against SEC EDGAR (public, no key). Skipped unless SEC_LIVE_TESTS=1 and
SEC_USER_AGENT (your name and contact email) are set:

    SEC_LIVE_TESTS=1 pytest -m live -s tests/integration/test_sec_live.py
"""

import os
from datetime import UTC, datetime

import pytest

from apps.alphascout.analysis.fundamentals import FactSource, extract_fundamentals
from apps.marketpilot.config import Settings
from packages.sec.client import SecEdgarClient
from packages.sec.parsing import parse_submissions, parse_ticker_map

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(os.environ.get("SEC_LIVE_TESTS") != "1", reason="set SEC_LIVE_TESTS=1"),
]

APPLE = 320193


def sec() -> SecEdgarClient:
    settings = Settings()
    if not settings.sec_user_agent:
        pytest.skip("SEC_USER_AGENT is not set")
    return SecEdgarClient.create(user_agent=settings.sec_user_agent)


async def test_ticker_map_submissions_and_facts_parse() -> None:
    client = sec()
    try:
        tickers = parse_ticker_map((await client.ticker_map()).payload)
        profile = parse_submissions((await client.submissions(APPLE)).payload)
        facts = await client.company_facts(APPLE)
    finally:
        await client.aclose()
    assert tickers["AAPL"].cik == APPLE
    assert profile.name.lower().startswith("apple") and "Nasdaq" in profile.exchanges
    assert any(f.form == "10-K" for f in profile.filings)
    now = datetime.now(UTC)
    fundamentals = extract_fundamentals(
        FactSource(facts.payload, APPLE, facts.url, facts.retrieved_at, now.date())
    )
    assert fundamentals.value("revenue") and fundamentals.value("revenue") > 0
    assert fundamentals.evidence["revenue"].source_url.startswith(
        "https://www.sec.gov/Archives/edgar/data/320193/"
    )
    print(f"\nmissing={fundamentals.missing} notes={fundamentals.notes}")
    print({k: str(e.value) for k, e in fundamentals.evidence.items()})


async def test_filing_document_text() -> None:
    client = sec()
    try:
        profile = parse_submissions((await client.submissions(APPLE)).payload)
        tenk = next(f for f in profile.filings if f.form == "10-K")
        document = await client.get_text(tenk.url)
    finally:
        await client.aclose()
    assert len(document.payload["text"]) > 10_000
    assert "<" not in document.payload["text"][:1000]
