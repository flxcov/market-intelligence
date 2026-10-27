"""End-to-end live research run: real Alpaca bars and news, real SEC data, test database.

Needs ALPACA_LIVE_TESTS=1, SEC_LIVE_TESTS=1, Alpaca keys, SEC_USER_AGENT and a migrated
TEST_DATABASE_URL (name ending in _test):

    ALPACA_LIVE_TESTS=1 SEC_LIVE_TESTS=1 pytest -m live -s tests/integration/test_alphascout_live.py
"""

import os
from datetime import timedelta

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from apps.alphascout.services.research import ResearchRequest, ResearchService
from apps.marketpilot.config import Settings
from apps.marketpilot.market_data import build_market_data_provider
from apps.marketpilot.services.ingestion import BarIngestionService
from packages.database.engine import create_session_factory
from packages.market_data.models import Adjustment, BarsRequest
from packages.sec.client import SecEdgarClient
from packages.shared.timeutils import utc_now
from tests.support import get_test_database_url

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("ALPACA_LIVE_TESTS") != "1" or os.environ.get("SEC_LIVE_TESTS") != "1",
        reason="set ALPACA_LIVE_TESTS=1 and SEC_LIVE_TESTS=1",
    ),
]


async def test_live_research_report() -> None:
    settings = Settings()
    test_db = get_test_database_url()
    if settings.missing_alpaca_credentials() or not settings.sec_user_agent or not test_db:
        pytest.skip("needs Alpaca keys, SEC_USER_AGENT and TEST_DATABASE_URL")
    if not (make_url(test_db).database or "").endswith("_test"):
        pytest.skip("TEST_DATABASE_URL database name must end in _test")

    provider = build_market_data_provider(settings)
    sec = SecEdgarClient.create(user_agent=settings.sec_user_agent)
    engine = create_async_engine(test_db, poolclass=NullPool)
    sessions = create_session_factory(engine)
    end = utc_now()
    try:
        await BarIngestionService(provider, sessions).ingest(
            BarsRequest(
                symbol="AAPL",
                start=end - timedelta(days=60),
                end=end,
                adjustment=Adjustment.RAW,
            )
        )
        service = ResearchService(sec, sessions, feed=settings.alpaca_data_feed, news=provider)
        stored = await service.research("AAPL", ResearchRequest())
    finally:
        await provider.aclose()
        await sec.aclose()
        await engine.dispose()

    report = stored.report
    assert report.cik == 320193 and report.evidence
    assert all(
        e.source_url.startswith("https://www.sec.gov/")
        for e in report.evidence
        if e.verification == "primary_source"
    )
    assert {c.date_status for c in report.upcoming_catalysts} <= {
        "confirmed",
        "estimated",
        "reported",
        "rumored",
    }
    # Mega-caps fall outside the default small-cap screen: that is expected, not a failure.
    print(f"\nverdict={report.verdict} reasons={report.verdict_reasons}")
    print(f"screen={report.screen.reasons} news={report.news_note}")
    print(f"upcoming={[(c.event_type, c.date_status) for c in report.upcoming_catalysts]}")
