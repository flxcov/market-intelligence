import pytest

_APP_ENV_VARS = (
    "APP_ENV",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "TRADING_MODE",
    "MARKET_DATA_PROVIDER",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_DATA_BASE_URL",
    "ALPACA_DATA_FEED",
    "ALPACA_TIMEOUT_SECONDS",
    "ALPACA_MAX_RETRIES",
    "DATABASE_URL",
    "API_WRITE_TOKEN",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Keep the developer's real environment (and keys) out of unit tests."""
    if request.node.get_closest_marker("live"):
        return
    for name in _APP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
