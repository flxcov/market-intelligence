"""Render the dashboard on real (SYNTHETIC) data through the real API and database."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from streamlit.testing.v1 import AppTest

from apps.alphascout.main import create_app as create_alphascout
from apps.dashboard import api_client
from apps.dashboard.app import PAGES
from apps.marketpilot.config import Settings
from apps.marketpilot.main import create_app
from apps.marketpilot.services.backtests import BacktestRequest, BacktestService
from apps.marketpilot.services.forward_trading import (
    CreateDeploymentRequest,
    ForwardTradingService,
    RunCycleRequest,
)
from apps.marketpilot.services.paper_models import CreateAccountRequest
from packages.database.engine import create_session_factory
from packages.database.market_bars import SeriesKey, upsert_bars
from packages.market_data.models import Adjustment, DataFeed
from tests.fakes import FakeProvider
from tests.strategy_data import backtest_scenario
from tests.support import PROJECT_ROOT

APP = str(PROJECT_ROOT / "apps" / "dashboard" / "app.py")
BARS = backtest_scenario()


async def seed(database_url: str) -> None:
    """Forward-trade the scenario day by day (one round trip) and run one backtest."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    sessions = create_session_factory(engine)
    for adjustment in (Adjustment.RAW, Adjustment.SPLIT):
        async with sessions.begin() as session:
            await upsert_bars(
                session,
                SeriesKey("SYN", "1Day", DataFeed.IEX, adjustment),
                BARS,
                provider="synthetic",
                retrieved_at=BARS[-1].timestamp,
            )
    clock = {"now": BARS[198].timestamp + timedelta(days=1, hours=1)}
    forward = ForwardTradingService(sessions, default_feed=DataFeed.IEX, now=lambda: clock["now"])
    account = await forward._paper.create_account(
        CreateAccountRequest(name="dash", starting_cash=Decimal(100_000))
    )
    deployment = await forward.create(
        CreateDeploymentRequest(account_id=account.id, name="trend", symbols=["SYN"])
    )
    for index in range(198, len(BARS)):
        clock["now"] = BARS[index].timestamp + timedelta(days=1, hours=1)
        await forward.run_cycle(deployment.id, RunCycleRequest(as_of=clock["now"], ingest=False))
    await BacktestService(sessions, default_feed=DataFeed.IEX).run(
        BacktestRequest(
            symbols=["SYN"],
            start=BARS[0].timestamp,
            end=BARS[-1].timestamp,
            adjustment="split",
            benchmark=None,
        )
    )
    await engine.dispose()


def test_dashboard_renders_real_data_and_only_reads(
    database_url: str, migrated_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(seed(database_url))
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, log_format="console", database_url=database_url, api_write_token="x" * 20
    )
    methods: list[str] = []

    def record(request: httpx.Request) -> None:
        methods.append(request.method)

    with (
        TestClient(create_app(settings, provider=FakeProvider())) as http,
        TestClient(create_alphascout(settings)) as research,
    ):
        http.event_hooks["request"].append(record)
        research.event_hooks["request"].append(record)
        monkeypatch.setattr(api_client, "get_client", lambda: api_client.DashboardClient(http))
        monkeypatch.setattr(
            api_client, "get_research_client", lambda: api_client.DashboardClient(research)
        )
        app = AppTest.from_file(APP, default_timeout=60).run()
        seen = {}
        for page in PAGES:
            app.sidebar.radio[0].set_value(page).run()
            assert not app.exception, (page, app.exception)
            assert [e.value for e in app.error] == [], page
            seen[page] = {
                "metrics": {m.label: m.value for m in app.metric},
                "warnings": [w.value for w in app.warning],
                "tables": len(app.dataframe),
            }

    assert set(methods) == {"GET"}, "the dashboard must never change state"
    assert seen["PAPER account"]["metrics"]["Equity"] == "$100,068.31"
    assert seen["PAPER account"]["metrics"]["Realized P&L"] == "$68.31"
    assert seen["Deployments"]["metrics"]["Status"] == "active"
    assert seen["Backtests"]["metrics"]["Closed trades"] == "1"
    assert any("Survivorship bias" in w for w in seen["Backtests"]["warnings"])
    assert seen["System health"]["metrics"]["Trading mode"] == "PAPER"
    assert seen["System health"]["metrics"]["Live trading"] == "disabled"
    # Coverage table only: the cycles ran without ingestion, so there are no ingestion runs.
    assert seen["Market data"]["tables"] == 1
    assert seen["Signals"]["tables"] >= 2  # signal list + rule checks
