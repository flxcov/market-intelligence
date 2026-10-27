"""Persistence endpoints without a database: auth, validation and configuration errors."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.marketpilot.config import Settings
from apps.marketpilot.main import create_app
from tests.fakes import FakeProvider

TOKEN = "test-write-token-0123456789"
INGEST_URL = "/market/bars/ingest"
BODY = {"symbol": "AAPL", "start": "2024-01-02", "end": "2024-01-05"}


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, log_format="console", **overrides)  # type: ignore[call-arg]


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(settings(api_write_token=TOKEN), provider=FakeProvider())
    with TestClient(app) as test_client:
        yield test_client


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_ingest_is_disabled_without_configured_token() -> None:
    with TestClient(create_app(settings(), provider=FakeProvider())) as client:
        response = client.post(INGEST_URL, json=BODY, headers=auth())
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "writes_disabled"


@pytest.mark.parametrize(
    "headers",
    [{}, auth("wrong-token-0123456789"), {"Authorization": TOKEN}, {"Authorization": "Basic x"}],
)
def test_ingest_rejects_missing_or_wrong_token(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post(INGEST_URL, json=BODY, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthorized"
    assert TOKEN not in response.text


def test_auth_is_checked_before_body_validation(client: TestClient) -> None:
    assert client.post(INGEST_URL, json={"symbol": "!!"}).status_code == 401


def test_ingest_validates_body(client: TestClient) -> None:
    response = client.post(
        INGEST_URL, json={**BODY, "start": "2024-01-02T09:30:00"}, headers=auth()
    )
    assert response.status_code == 422


def test_ingest_without_database_returns_clear_503(client: TestClient) -> None:
    response = client.post(INGEST_URL, json=BODY, headers=auth())
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["type"] == "database_not_configured"
    assert "DATABASE_URL" in error["message"]


@pytest.mark.parametrize(
    "url",
    ["/market/bars/stored?symbol=AAPL&start=2024-01-02&end=2024-01-05", "/market/ingestion-runs"],
)
def test_read_endpoints_without_database_return_503(client: TestClient, url: str) -> None:
    response = client.get(url)
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "database_not_configured"


def test_stored_bars_validates_query_before_database_check(client: TestClient) -> None:
    response = client.get("/market/bars/stored?symbol=AAPL&start=bad&end=2024-01-05")
    assert response.status_code == 422


def test_health_reports_writes_enabled(client: TestClient) -> None:
    assert client.get("/health").json()["writes_enabled"] is True


def test_unreachable_database_is_reported_by_health() -> None:
    # Port 1 on localhost: nothing listens there, so the connection is refused quickly.
    app = create_app(
        settings(database_url="postgresql+psycopg://user:pw-secret-123@127.0.0.1:1/nodb"),
        provider=FakeProvider(),
    )
    with TestClient(app) as client:
        body = client.get("/health").json()
        stored = client.get("/market/bars/stored?symbol=AAPL&start=2024-01-02&end=2024-01-05")
    assert body["status"] == "degraded"
    assert body["database"]["configured"] is True
    assert body["database"]["reachable"] is False
    assert "pw-secret-123" not in str(body)
    assert stored.status_code == 503
    assert stored.json()["error"]["type"] == "database_unavailable"
    assert "pw-secret-123" not in stored.text


def test_every_post_endpoint_requires_the_write_token() -> None:
    """Safety net for new routes: no state-changing endpoint is reachable without a token."""
    with TestClient(create_app(settings(), provider=FakeProvider())) as client:
        schema = client.get("/openapi.json").json()
        posts = [path for path, ops in schema["paths"].items() if "post" in ops]
        assert len(posts) >= 15
        for path in posts:
            url = path.replace("{account_id}", "1").replace("{order_id}", "1")
            url = url.replace("{deployment_id}", "1").replace("{kind}/{subject_id}", "order/1")
            response = client.post(url, json={})
            assert response.status_code == 403, path
            assert response.json()["error"]["type"] == "writes_disabled", path
