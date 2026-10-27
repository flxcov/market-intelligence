"""Alembic environment.

Migrations run synchronously with the same psycopg driver the app uses (psycopg 3
supports both sync and async), which keeps this file simple.

URL resolution: `config.attributes["database_url"]` (set by tests) or the
application's DATABASE_URL setting.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from apps.marketpilot.config import Settings
from packages.database.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = config.attributes.get("database_url")
    if url:
        return url
    settings = Settings()
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is not set. Add it to .env (see .env.example).")
    return settings.database_url.get_secret_value()


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (`alembic upgrade head --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
