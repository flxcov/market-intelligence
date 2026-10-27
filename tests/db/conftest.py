"""Fixtures for tests that need a real PostgreSQL database.

Set TEST_DATABASE_URL (environment or .env) to a *dedicated* test database whose
name ends in "_test"; every table in it is wiped between tests. Without it, these
tests skip.
"""

import importlib.util
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from packages.database.engine import create_session_factory
from tests.support import PROJECT_ROOT, get_test_database_url

# The dashboard test needs the optional [dashboard] extra (Streamlit).
collect_ignore = [] if importlib.util.find_spec("streamlit") else ["test_dashboard.py"]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/db/" in item.nodeid:
            item.add_marker(pytest.mark.db)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = get_test_database_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    name = make_url(url).database or ""
    if not name.endswith("_test"):
        pytest.exit(
            f"Refusing to run: TEST_DATABASE_URL database {name!r} must end in '_test' "
            "because tests delete all of its data.",
            returncode=2,
        )
    return url


def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    config.attributes["configure_logging"] = False
    return config


@pytest.fixture(scope="session")
def migrated_engine(database_url: str) -> Iterator[Engine]:
    """Rebuild the schema from scratch through the real migrations, once per session."""
    config = alembic_config(database_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(database_url, poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(migrated_engine: Engine) -> Iterator[None]:
    yield
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE market_bars, ingestion_runs, signals, strategy_configs, "
                "paper_accounts, paper_orders, paper_fills, paper_ledger, paper_positions, "
                "paper_equity_snapshots, backtests, paper_deployments, trading_cycles, "
                "option_quotes, ai_explanations, research_evidence, research_catalysts, "
                "research_reports, research_documents, news_items "
                "RESTART IDENTITY"
            )
        )


@pytest.fixture
async def sessions(
    database_url: str, migrated_engine: Engine
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    yield create_session_factory(engine)
    await engine.dispose()
