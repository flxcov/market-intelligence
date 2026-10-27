"""Validation of signal-generation requests and the API without a database."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.marketpilot.config import Settings
from apps.marketpilot.main import create_app
from apps.marketpilot.services.signals import SignalGenerationRequest
from packages.market_data.models import Adjustment
from tests.fakes import FakeProvider

TOKEN = "test-write-token-0123456789"


def test_defaults_are_split_adjusted_daily_trend_following() -> None:
    request = SignalGenerationRequest(symbols=["aapl", "MSFT", "AAPL"])
    assert request.symbols == ["AAPL", "MSFT"]  # normalized and de-duplicated, order kept
    assert request.adjustment is Adjustment.SPLIT
    assert (request.strategy, request.timeframe, request.as_of) == ("trend_following", "1Day", None)


def test_raw_adjustment_is_rejected_with_explanation() -> None:
    with pytest.raises(ValidationError, match="split would look like a price crash"):
        SignalGenerationRequest(symbols=["AAPL"], adjustment="raw")


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbols": []},
        {"symbols": [f"S{i}" for i in range(51)]},
        {"symbols": ["!!"]},
        {"timeframe": "1Hour"},
        {"strategy": "momentum"},
        {"as_of": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        {"as_of": "2024-01-02T10:00:00"},  # naive
        {"parameters": {"fast_ma_period": 300, "slow_ma_period": 200}},
        {"parameters": {"unknown": 1}},
        {"extra_field": True},
    ],
)
def test_invalid_requests_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SignalGenerationRequest(**{"symbols": ["AAPL"], **overrides})


def test_as_of_is_normalized_to_utc() -> None:
    request = SignalGenerationRequest(symbols=["AAPL"], as_of="2024-01-02T09:00:00-05:00")
    assert request.as_of == datetime(2024, 1, 2, 14, tzinfo=UTC)


def make_client(**overrides: object) -> TestClient:
    settings = Settings(_env_file=None, log_format="console", **overrides)  # type: ignore[call-arg]
    return TestClient(create_app(settings, provider=FakeProvider()))


def test_generate_requires_write_token() -> None:
    with make_client(api_write_token=TOKEN) as client:
        response = client.post("/signals/generate", json={"symbols": ["AAPL"]})
    assert response.status_code == 401


def test_generate_without_database_returns_503() -> None:
    with make_client(api_write_token=TOKEN) as client:
        response = client.post(
            "/signals/generate",
            json={"symbols": ["AAPL"]},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "database_not_configured"


def test_list_signals_without_database_returns_503() -> None:
    with make_client() as client:
        assert client.get("/signals").status_code == 503
