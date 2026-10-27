"""End-to-end API tests against a real database (provider is a synthetic fake)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from apps.marketpilot.config import Settings
from apps.marketpilot.main import create_app
from packages.market_data.errors import ProviderUnavailableError
from tests.fakes import FakeProvider, synthetic_daily_bars

TOKEN = "test-write-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BODY = {"symbol": "test", "timeframe": "1Day", "start": "2024-01-01", "end": "2024-01-31"}
STORED_URL = "/market/bars/stored?symbol=TEST&start=2024-01-01&end=2024-01-31"


def make_client(database_url: str, provider: FakeProvider) -> TestClient:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, log_format="console", database_url=database_url, api_write_token=TOKEN
    )
    return TestClient(create_app(settings, provider=provider))


@pytest.fixture
def client(database_url: str, migrated_engine: Engine) -> Iterator[TestClient]:
    with make_client(database_url, FakeProvider(synthetic_daily_bars(3))) as test_client:
        yield test_client


def test_health_is_ok_with_database_and_provider(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["database"] == {
        "configured": True,
        "reachable": True,
        "schema_revision": "0009",
        "detail": None,
    }


def test_ingest_then_read_back(client: TestClient) -> None:
    first = client.post("/market/bars/ingest", json=BODY, headers=AUTH)
    assert first.status_code == 200
    assert (first.json()["inserted"], first.json()["unchanged"]) == (3, 0)

    second = client.post("/market/bars/ingest", json=BODY, headers=AUTH)
    assert (second.json()["inserted"], second.json()["unchanged"]) == (0, 3)

    stored = client.get(STORED_URL).json()
    assert stored["source"] == "database"
    assert stored["bar_count"] == 3
    assert stored["truncated"] is False
    first_bar = stored["bars"][0]
    assert first_bar["open"] == "100.10"  # exact decimal string survives the round trip
    assert first_bar["timestamp"] == "2024-01-02T05:00:00Z"
    assert (first_bar["provider"], first_bar["revision"]) == ("fake", 1)


def test_stored_bars_truncation_and_empty_result(client: TestClient) -> None:
    client.post("/market/bars/ingest", json=BODY, headers=AUTH)

    truncated = client.get(STORED_URL + "&max_bars=2").json()
    assert (truncated["bar_count"], truncated["truncated"]) == (2, True)

    empty = client.get(STORED_URL.replace("TEST", "OTHER")).json()
    assert empty["bars"] == []
    assert any("Ingest them first" in notice for notice in empty["notices"])


def test_failed_ingestion_is_listed(database_url: str, migrated_engine: Engine) -> None:
    provider = FakeProvider(error=ProviderUnavailableError("Alpaca returned HTTP 503"))
    with make_client(database_url, provider) as test_client:
        response = test_client.post("/market/bars/ingest", json=BODY, headers=AUTH)
        runs = test_client.get("/market/ingestion-runs?symbol=test").json()

    assert response.status_code == 503
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["error_type"] == "provider_unavailable"


def test_ingestion_runs_are_newest_first(client: TestClient) -> None:
    for _ in range(3):
        client.post("/market/bars/ingest", json=BODY, headers=AUTH)
    ids = [run["id"] for run in client.get("/market/ingestion-runs?limit=2").json()]
    assert ids == [3, 2]


# --- signals (Phase 3) ------------------------------------------------------------


def test_generate_and_list_signals(client: TestClient) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from packages.database.engine import create_session_factory
    from packages.database.market_bars import SeriesKey, upsert_bars
    from packages.market_data.models import Adjustment, DataFeed
    from tests.strategy_data import as_of_after, breakout_series

    bars = breakout_series()
    database_url = client.app.state.settings.database_url.get_secret_value()

    async def seed() -> None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        async with create_session_factory(engine).begin() as session:
            await upsert_bars(
                session,
                SeriesKey("UP", "1Day", DataFeed.IEX, Adjustment.SPLIT),
                bars,
                provider="fake",
                retrieved_at=bars[-1].timestamp,
            )
        await engine.dispose()

    asyncio.run(seed())
    body = {"symbols": ["up"], "as_of": as_of_after(bars).isoformat()}

    response = client.post("/signals/generate", json=body, headers=AUTH)
    assert response.status_code == 200
    result = response.json()
    assert (result["strategy"], result["version"]) == ("trend_following", "1.0.0")
    item = result["results"][0]
    assert item["evaluation"]["outcome"] == "signal"
    assert item["evaluation"]["proposal"]["exit"]["initial_stop"] == "139.5857"

    again = client.post("/signals/generate", json=body, headers=AUTH).json()["results"][0]
    assert (again["signal_id"], again["already_existed"]) == (item["signal_id"], True)

    listed = client.get("/signals?symbol=UP").json()
    assert len(listed) == 1
    assert listed[0]["reference_price"] == "141.8"
    assert listed[0]["strategy_version"] == "1.0.0"
    assert listed[0]["rule_checks"][2]["rule"] == "breakout"


def test_generate_rejects_raw_adjustment(client: TestClient) -> None:
    response = client.post(
        "/signals/generate", json={"symbols": ["UP"], "adjustment": "raw"}, headers=AUTH
    )
    assert response.status_code == 422


# --- paper trading (Phase 4) -----------------------------------------------------------


def test_paper_trading_over_http(client: TestClient) -> None:
    created = client.post(
        "/paper/accounts", json={"name": "http", "starting_cash": "50000"}, headers=AUTH
    )
    assert created.status_code == 200
    account = created.json()
    assert (account["mode"], account["equity"], account["status"]) == ("PAPER", "50000", "active")
    account_id = account["id"]

    assert (
        client.post("/paper/accounts", json={"name": "x", "starting_cash": "1"}).status_code == 401
    )
    assert client.get("/paper/accounts/999").status_code == 404
    duplicate = client.post(
        "/paper/accounts", json={"name": "http", "starting_cash": "1"}, headers=AUTH
    )
    assert duplicate.status_code == 409

    # Buy without a protective stop is invalid input.
    no_stop = {"symbol": "TEST", "side": "buy", "quantity": 1}
    assert (
        client.post(f"/paper/accounts/{account_id}/orders", json=no_stop, headers=AUTH).status_code
        == 422
    )

    # No market data for the symbol: the order is recorded as rejected, with reasons.
    order = client.post(
        f"/paper/accounts/{account_id}/orders",
        json={**no_stop, "protective_stop_price": "1"},
        headers=AUTH,
    ).json()
    assert (order["mode"], order["accepted"], order["order"]["status"]) == (
        "PAPER",
        False,
        "rejected",
    )
    assert order["order"]["risk_checks"][0]["rule"] == "market_data_available"

    processed = client.post(f"/paper/accounts/{account_id}/process", headers=AUTH).json()
    assert processed["mode"] == "PAPER"
    assert processed["events"] == []

    reconcile = client.get(f"/paper/accounts/{account_id}/reconcile").json()
    assert reconcile["balanced"] is True
    ledger = client.get(f"/paper/accounts/{account_id}/ledger").json()
    assert [(e["kind"], e["amount"], e["mode"]) for e in ledger] == [("deposit", "50000", "PAPER")]

    halted = client.post(f"/paper/accounts/{account_id}/halt", headers=AUTH).json()
    assert halted["status"] == "halted"
    assert (
        client.post(f"/paper/accounts/{account_id}/resume", headers=AUTH).json()["status"]
        == "active"
    )
    listed = client.get("/paper/accounts").json()
    assert [(a["id"], a["mode"]) for a in listed] == [(account_id, "PAPER")]


def test_paper_cancel_errors(client: TestClient) -> None:
    account_id = client.post(
        "/paper/accounts", json={"name": "c", "starting_cash": "1000"}, headers=AUTH
    ).json()["id"]
    missing = client.post(f"/paper/accounts/{account_id}/orders/5/cancel", headers=AUTH)
    assert (missing.status_code, missing.json()["error"]["type"]) == (404, "not_found")


# --- backtests (Phase 5) ----------------------------------------------------------------


def test_backtests_over_http(client: TestClient) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from packages.database.engine import create_session_factory
    from packages.database.market_bars import SeriesKey, upsert_bars
    from packages.market_data.models import Adjustment, DataFeed
    from tests.strategy_data import backtest_scenario

    bars = backtest_scenario()
    database_url = client.app.state.settings.database_url.get_secret_value()

    async def seed() -> None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        async with create_session_factory(engine).begin() as session:
            await upsert_bars(
                session,
                SeriesKey("SYN", "1Day", DataFeed.IEX, Adjustment.ALL),
                bars,
                provider="synthetic",
                retrieved_at=bars[-1].timestamp,
            )
        await engine.dispose()

    asyncio.run(seed())
    body = {
        "symbols": ["syn"],
        "start": bars[0].timestamp.isoformat(),
        "end": bars[-1].timestamp.isoformat(),
        "benchmark": None,
    }
    assert client.post("/backtests", json=body).status_code == 401
    assert (
        client.post("/backtests", json={**body, "adjustment": "raw"}, headers=AUTH).status_code
        == 422
    )

    response = client.post("/backtests", json=body, headers=AUTH)
    assert response.status_code == 200
    report = response.json()
    assert report["mode"] == "BACKTEST"
    assert report["metrics"]["trades"] == 1
    assert report["trades"][0]["net_pnl"] == "68.31"
    assert report["disclaimer"].startswith("Hypothetical")

    fetched = client.get(f"/backtests/{report['id']}").json()
    assert fetched["data_fingerprint"] == report["data_fingerprint"]
    assert fetched["equity_curve"][-1]["equity"] == "100068.31"
    assert [b["id"] for b in client.get("/backtests").json()] == [report["id"]]
    assert client.get("/backtests/999").status_code == 404

    no_data = client.post("/backtests", json={**body, "symbols": ["NONE"]}, headers=AUTH)
    assert (no_data.status_code, no_data.json()["error"]["type"]) == (
        400,
        "invalid_backtest_request",
    )


# --- forward trading deployments (Phase 6) ---------------------------------------------------


def test_deployments_over_http(client: TestClient) -> None:
    account_id = client.post(
        "/paper/accounts", json={"name": "fwd", "starting_cash": "10000"}, headers=AUTH
    ).json()["id"]
    body = {"account_id": account_id, "name": "trend", "symbols": ["aapl", "msft", "AAPL"]}
    assert client.post("/paper/deployments", json=body).status_code == 401

    created = client.post("/paper/deployments", json=body, headers=AUTH).json()
    assert (created["mode"], created["status"], created["symbols"]) == (
        "PAPER",
        "active",
        ["AAPL", "MSFT"],
    )
    deployment_id = created["id"]
    assert client.post("/paper/deployments", json=body, headers=AUTH).status_code == 409

    cycle = client.post(
        f"/paper/deployments/{deployment_id}/cycle", json={"ingest": False}, headers=AUTH
    ).json()
    assert (cycle["mode"], cycle["status"]) == ("PAPER", "succeeded")
    assert cycle["summary"]["signals"] == {"AAPL": "insufficient_data", "MSFT": "insufficient_data"}
    history = client.get(f"/paper/deployments/{deployment_id}/cycles").json()
    assert [c["id"] for c in history] == [cycle["id"]]

    assert (
        client.post(f"/paper/deployments/{deployment_id}/pause", headers=AUTH).json()["status"]
        == "paused"
    )
    assert (
        client.post(f"/paper/deployments/{deployment_id}/resume", headers=AUTH).json()["status"]
        == "active"
    )
    shut = client.post(
        f"/paper/deployments/{deployment_id}/shutdown", json={"liquidate": True}, headers=AUTH
    ).json()
    assert (shut["deployment"]["status"], shut["liquidation_orders"]) == ("stopped", [])
    assert client.get(f"/paper/accounts/{account_id}").json()["status"] == "halted"
    resumed = client.post(f"/paper/deployments/{deployment_id}/resume", headers=AUTH)
    assert resumed.status_code == 409
    assert client.get("/paper/deployments/999").status_code == 404


# --- dashboard data endpoints (Phase 7) --------------------------------------------------


def test_alerts_coverage_and_equity_endpoints(client: TestClient) -> None:
    account_id = client.post(
        "/paper/accounts", json={"name": "dash", "starting_cash": "1000"}, headers=AUTH
    ).json()["id"]
    client.post("/market/bars/ingest", json=BODY, headers=AUTH)  # FakeProvider: 3 bars

    coverage = client.get("/market/coverage").json()
    assert [(c["symbol"], c["adjustment"], c["bars"]) for c in coverage] == [("TEST", "raw", 3)]
    assert coverage[0]["last_bar"] == "2024-01-04T05:00:00Z"
    assert client.get("/market/coverage?symbol=OTHER").json() == []

    assert client.get(f"/paper/accounts/{account_id}/equity").json() == []
    assert client.get("/paper/accounts/999/equity").status_code == 404

    report = client.get("/alerts").json()
    assert report["counts"]["critical"] == 0
    assert isinstance(report["alerts"], list)
