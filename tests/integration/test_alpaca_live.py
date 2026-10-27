"""Live check against the real Alpaca API.

Skipped unless ALPACA_LIVE_TESTS=1 and credentials are set. Run with:
    ALPACA_LIVE_TESTS=1 pytest -m live
"""

import os
from datetime import UTC, datetime

import pytest

from apps.marketpilot.config import Settings
from apps.marketpilot.market_data import build_market_data_provider
from packages.market_data.models import BarsRequest
from tests.support import get_test_database_url

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("ALPACA_LIVE_TESTS") != "1", reason="set ALPACA_LIVE_TESTS=1 to run"
    ),
]


async def test_fetch_real_daily_bars() -> None:
    settings = Settings()
    if settings.missing_alpaca_credentials():
        pytest.skip(f"missing {settings.missing_alpaca_credentials()}")
    provider = build_market_data_provider(settings)
    try:
        series = await provider.get_bars(
            BarsRequest(symbol="AAPL", timeframe="1Day", start="2024-01-02", end="2024-01-31")
        )
    finally:
        await provider.aclose()

    # January 2024 had 20 NYSE trading days; the IEX feed should have a bar for each.
    assert 15 <= series.bar_count <= 21
    assert all(b.timestamp.tzinfo == UTC for b in series.bars)
    assert series.bars[0].timestamp >= datetime(2024, 1, 2, tzinfo=UTC)
    print(f"\n{series.bar_count} bars, first={series.bars[0]!r}")  # visible with -s


async def test_ingest_real_bars_is_idempotent() -> None:
    """Phase 2 milestone check: real bars persist, and re-ingesting changes nothing.

    Also needs TEST_DATABASE_URL (a migrated database whose name ends in _test).
    """
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from apps.marketpilot.services.ingestion import BarIngestionService
    from packages.database.engine import create_session_factory

    settings = Settings()
    test_db = get_test_database_url()
    if settings.missing_alpaca_credentials() or not test_db:
        pytest.skip("needs Alpaca credentials and TEST_DATABASE_URL")
    if not (make_url(test_db).database or "").endswith("_test"):
        pytest.skip("TEST_DATABASE_URL database name must end in _test")

    provider = build_market_data_provider(settings)
    engine = create_async_engine(test_db, poolclass=NullPool)
    service = BarIngestionService(provider, create_session_factory(engine))
    request = BarsRequest(symbol="AAPL", timeframe="1Day", start="2024-01-02", end="2024-01-31")
    try:
        first = await service.ingest(request)
        second = await service.ingest(request)
    finally:
        await provider.aclose()
        await engine.dispose()

    assert first.bars_received >= 15
    assert first.inserted + first.unchanged == first.bars_received  # first run may follow others
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, second.bars_received)
    print(f"\nfirst={first.model_dump(include={'inserted', 'unchanged'})} second=unchanged")


async def test_fetch_real_news() -> None:
    from datetime import timedelta

    from packages.market_data.news import NewsRequest
    from packages.shared.timeutils import utc_now

    settings = Settings()
    if settings.missing_alpaca_credentials():
        pytest.skip(f"missing {settings.missing_alpaca_credentials()}")
    provider = build_market_data_provider(settings)
    end = utc_now()
    try:
        batch = await provider.get_news(
            NewsRequest(symbol="AAPL", start=end - timedelta(days=7), end=end, max_articles=20)
        )
    finally:
        await provider.aclose()
    # AAPL has news most weeks; an empty week is possible, so only check what came back.
    for article in batch.articles:
        assert article.created_at.tzinfo is not None and article.headline
    print(f"\n{len(batch.articles)} articles; first={batch.articles[:1]!r}")


async def test_fetch_real_option_chain() -> None:
    from packages.market_data.options import OptionChainRequest

    settings = Settings()
    if settings.missing_alpaca_credentials():
        pytest.skip(f"missing {settings.missing_alpaca_credentials()}")
    provider = build_market_data_provider(settings)
    try:
        chain = await provider.get_option_chain(
            OptionChainRequest(
                underlying="AAPL", feed=settings.alpaca_options_feed, max_contracts=50
            )
        )
    finally:
        await provider.aclose()
    assert chain.quotes or chain.skipped, chain.notices
    assert all(q.contract.root == "AAPL" for q in chain.quotes)
    print(f"\n{len(chain.quotes)} quotes, {len(chain.skipped)} skipped; feed={chain.feed}")
