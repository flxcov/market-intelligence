from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.db.conftest import alembic_config

INSERT_BAR = text(
    """
    INSERT INTO market_bars (symbol, timeframe, feed, adjustment, timestamp,
        open, high, low, close, volume, provider, retrieved_at)
    VALUES ('TEST', '1Day', 'iex', 'raw', :ts, :o, :h, :l, :c, :v, 'fake', :ts)
    """
)
TS = datetime(2024, 1, 2, 5, tzinfo=UTC)


def test_models_and_migrations_have_not_drifted(database_url: str, migrated_engine: Engine) -> None:
    # Raises if autogenerate would produce any operation.
    command.check(alembic_config(database_url))


def test_schema_has_expected_tables_and_key(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    assert {
        "market_bars",
        "ingestion_runs",
        "strategy_configs",
        "signals",
        "alembic_version",
    } <= set(inspector.get_table_names())
    unique = [
        (uc["name"], uc["column_names"]) for uc in inspector.get_unique_constraints("market_bars")
    ]
    assert unique == [
        ("uq_market_bars_key", ["symbol", "timeframe", "feed", "adjustment", "timestamp"])
    ]


@pytest.mark.parametrize(
    "values",
    [
        {"o": 100, "h": 99, "l": 98, "c": 100, "v": 1},  # high below open
        {"o": 100, "h": 101, "l": 100.5, "c": 100.2, "v": 1},  # low above close
        {"o": 0, "h": 1, "l": 0, "c": 1, "v": 1},  # non-positive price
        {"o": 100, "h": 101, "l": 99, "c": 100, "v": -1},  # negative volume
    ],
)
def test_database_rejects_invalid_bars(migrated_engine: Engine, values: dict[str, float]) -> None:
    with pytest.raises(IntegrityError), migrated_engine.begin() as conn:
        conn.execute(INSERT_BAR, {"ts": TS, **values})


def test_database_rejects_duplicate_natural_key(migrated_engine: Engine) -> None:
    valid = {"ts": TS, "o": 100, "h": 101, "l": 99, "c": 100, "v": 1}
    with migrated_engine.begin() as conn:
        conn.execute(INSERT_BAR, valid)
    with pytest.raises(IntegrityError), migrated_engine.begin() as conn:
        conn.execute(INSERT_BAR, valid)
