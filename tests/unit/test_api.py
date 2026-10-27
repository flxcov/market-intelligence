"""API tests. A fake provider stands in for Alpaca; no network calls are made."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.marketpilot.config import Settings
from apps.marketpilot.main import create_app
from packages.market_data.errors import (
    MarketDataError,
    ProviderAccessError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from packages.market_data.models import Adjustment, DataFeed
from tests.fakes import FakeProvider

BARS_URL = "/market/bars"
PAPER_PATHS = {
    "/paper/accounts",
    "/paper/accounts/{account_id}",
    "/paper/accounts/{account_id}/orders",
    "/paper/accounts/{account_id}/orders/from-signal",
    "/paper/accounts/{account_id}/options/orders",
    "/paper/accounts/{account_id}/orders/{order_id}/cancel",
    "/paper/accounts/{account_id}/process",
    "/paper/accounts/{account_id}/fills",
    "/paper/accounts/{account_id}/ledger",
    "/paper/accounts/{account_id}/reconcile",
    "/paper/accounts/{account_id}/halt",
    "/paper/accounts/{account_id}/resume",
    "/paper/accounts/{account_id}/equity",
    "/paper/deployments",
    "/paper/deployments/{deployment_id}",
    "/paper/deployments/{deployment_id}/cycles",
    "/paper/deployments/{deployment_id}/cycle",
    "/paper/deployments/{deployment_id}/pause",
    "/paper/deployments/{deployment_id}/resume",
    "/paper/deployments/{deployment_id}/shutdown",
}
VALID_QUERY = {"symbol": "aapl", "start": "2024-01-02", "end": "2024-01-05"}


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, log_format="console", **overrides)  # type: ignore[call-arg]


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def client(provider: FakeProvider) -> Iterator[TestClient]:
    with TestClient(create_app(settings(), provider=provider)) as test_client:
        yield test_client


@pytest.fixture
def unconfigured_client() -> Iterator[TestClient]:
    with TestClient(create_app(settings())) as test_client:
        yield test_client


# --- health --------------------------------------------------------------------


def test_health_when_configured(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    # Database not configured in unit tests, so the service reports "degraded".
    assert body["status"] == "degraded"
    assert body["database"]["configured"] is False
    assert body["writes_enabled"] is False
    assert body["trading_mode"] == "paper"
    assert body["live_trading_enabled"] is False
    assert body["market_data"] == {"provider": "alpaca", "configured": True, "detail": None}
    assert body["time_utc"].endswith("Z")


def test_health_without_credentials_explains_what_is_missing(
    unconfigured_client: TestClient,
) -> None:
    response = unconfigured_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["market_data"]["configured"] is False
    assert "ALPACA_API_KEY" in body["market_data"]["detail"]


def test_app_builds_real_provider_when_credentials_present() -> None:
    app = create_app(settings(alpaca_api_key="test-key-1234", alpaca_secret_key="test-sec-5678"))
    with TestClient(app) as test_client:
        assert test_client.get("/health").json()["market_data"]["configured"] is True


# --- bars: success -------------------------------------------------------------


def test_bars_success(client: TestClient, provider: FakeProvider) -> None:
    response = client.get(BARS_URL, params=VALID_QUERY)
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "AAPL"
    assert body["timeframe"] == "1Day"
    assert body["bar_count"] == 1
    assert body["bars"][0]["open"] == "100.10"  # decimal string, precision preserved
    assert body["bars"][0]["timestamp"] == "2024-01-02T05:00:00Z"
    assert body["start"] == "2024-01-02T00:00:00Z"
    assert body["end"] == "2024-01-05T23:59:59Z"

    sent = provider.requests[0]
    assert sent.symbol == "AAPL"
    assert sent.adjustment is Adjustment.RAW
    assert sent.feed is None


def test_bars_passes_optional_parameters(client: TestClient, provider: FakeProvider) -> None:
    query = {**VALID_QUERY, "timeframe": "5Min", "adjustment": "all", "feed": "sip"}
    response = client.get(BARS_URL, params={**query, "max_bars": 50})
    assert response.status_code == 200
    sent = provider.requests[0]
    assert (sent.timeframe, sent.adjustment, sent.feed, sent.max_bars) == (
        "5Min",
        Adjustment.ALL,
        DataFeed.SIP,
        50,
    )


# --- bars: input validation ----------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": "AAPL;DROP"},
        {"symbol": ""},
        {"timeframe": "1day"},
        {"start": "2024-01-02T09:30:00"},  # naive datetime
        {"start": "2024-02-01"},  # start after end
        {"start": "not-a-date"},
        {"feed": "otc"},
        {"adjustment": "magic"},
        {"max_bars": "0"},
        {"timeFrame": "1Day"},  # unknown parameter
    ],
)
def test_bars_rejects_invalid_input(
    client: TestClient, provider: FakeProvider, overrides: dict[str, str]
) -> None:
    response = client.get(BARS_URL, params={**VALID_QUERY, **overrides})
    assert response.status_code == 422
    assert provider.requests == []


@pytest.mark.parametrize("missing", ["symbol", "start", "end"])
def test_bars_requires_core_parameters(client: TestClient, missing: str) -> None:
    query = {k: v for k, v in VALID_QUERY.items() if k != missing}
    assert client.get(BARS_URL, params=query).status_code == 422


def test_invalid_input_is_reported_even_when_unconfigured(
    unconfigured_client: TestClient,
) -> None:
    response = unconfigured_client.get(BARS_URL, params={**VALID_QUERY, "symbol": "!!"})
    assert response.status_code == 422


# --- bars: provider failures ---------------------------------------------------


def test_bars_without_credentials_returns_clear_503(unconfigured_client: TestClient) -> None:
    response = unconfigured_client.get(BARS_URL, params=VALID_QUERY)
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["type"] == "provider_not_configured"
    assert "ALPACA_API_KEY" in error["message"]
    assert "bars" not in response.json()


@pytest.mark.parametrize(
    ("error", "status", "error_type"),
    [
        (ProviderAccessError("denied", provider="fake"), 502, "provider_access_denied"),
        (ProviderUnavailableError("down", provider="fake"), 503, "provider_unavailable"),
        (ProviderRateLimitError("slow down", provider="fake"), 429, "provider_rate_limited"),
    ],
)
def test_provider_errors_map_to_http_errors(
    error: MarketDataError, status: int, error_type: str
) -> None:
    with TestClient(create_app(settings(), provider=FakeProvider(error=error))) as test_client:
        response = test_client.get(BARS_URL, params=VALID_QUERY)
    assert response.status_code == status
    assert response.json() == {
        "error": {"type": error_type, "message": error.message, "provider": "fake"}
    }


def test_rate_limit_includes_retry_after_header() -> None:
    error = ProviderRateLimitError("slow down", provider="fake", retry_after=1.2)
    with TestClient(create_app(settings(), provider=FakeProvider(error=error))) as test_client:
        response = test_client.get(BARS_URL, params=VALID_QUERY)
    assert response.headers["Retry-After"] == "2"


# --- safety --------------------------------------------------------------------


def test_no_order_or_trading_routes_exist(client: TestClient) -> None:
    paths = set(client.get("/openapi.json").json()["paths"])
    assert paths == {
        "/health",
        "/market/bars",
        "/market/bars/ingest",
        "/market/bars/stored",
        "/market/ingestion-runs",
        "/signals",
        "/signals/generate",
        *PAPER_PATHS,
        "/backtests",
        "/backtests/{backtest_id}",
        "/market/coverage",
        "/market/options/chain",
        "/market/options/chain/ingest",
        "/market/options/contracts/{symbol}/quotes",
        "/alerts",
        "/explanations/{kind}/{subject_id}",
        "/explanations/{kind}/{subject_id}/attempts",
    }
    # The only order endpoints are PAPER (simulated) ones.
    assert all(path.startswith("/paper/") for path in paths if "order" in path)
