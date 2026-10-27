"""Render every dashboard page with Streamlit's AppTest (no browser, no server).

Here the API has no database configured (or is unreachable): every page must
still render, showing a clear message instead of crashing.
"""

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from apps.dashboard import api_client
from apps.dashboard.app import PAGES
from apps.marketpilot.config import Settings
from apps.marketpilot.main import create_app
from tests.fakes import FakeProvider
from tests.support import PROJECT_ROOT

APP = str(PROJECT_ROOT / "apps" / "dashboard" / "app.py")


def render_all(monkeypatch: pytest.MonkeyPatch, client: api_client.DashboardClient) -> dict:
    monkeypatch.setattr(api_client, "get_client", lambda: client)
    monkeypatch.setattr(api_client, "get_research_client", lambda: client)
    results = {}
    app = AppTest.from_file(APP, default_timeout=30).run()
    for page in PAGES:
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, (page, app.exception)
        results[page] = app
        results[page + ":errors"] = [e.value for e in app.error]
    return results


@pytest.fixture
def api_without_database() -> Iterator[TestClient]:
    settings = Settings(_env_file=None, log_format="console")  # type: ignore[call-arg]
    with TestClient(create_app(settings, provider=FakeProvider())) as client:
        yield client


def test_every_page_renders_without_a_database(
    monkeypatch: pytest.MonkeyPatch, api_without_database: TestClient
) -> None:
    results = render_all(monkeypatch, api_client.DashboardClient(api_without_database))
    assert any("database_not_configured" in e for e in results["Overview:errors"])
    # The health page works without a database and says what is missing.
    assert results["System health:errors"] == []


def test_every_page_renders_when_the_api_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request):
        raise httpx.ConnectError("refused")

    client = api_client.DashboardClient(
        httpx.Client(base_url="http://127.0.0.1:9", transport=httpx.MockTransport(refuse))
    )
    results = render_all(monkeypatch, client)
    for page in PAGES:
        assert any("not reachable" in e for e in results[page + ":errors"]), page
