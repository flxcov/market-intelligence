"""Async engine, session factory and connectivity checks."""

import asyncio
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

REQUIRED_URL_PREFIX = "postgresql+psycopg://"
HEALTH_CHECK_TIMEOUT_SECONDS = 2.0


def create_engine(database_url: str) -> AsyncEngine:
    """Create an engine. No connection is opened until the first query."""
    return create_async_engine(
        database_url,
        pool_pre_ping=True,  # transparently replace connections the server dropped
        pool_size=5,
        max_overflow=5,
        connect_args={"connect_timeout": 5},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False lets callers read ORM objects after the transaction ends.
    return async_sessionmaker(engine, expire_on_commit=False)


@dataclass(frozen=True)
class DatabaseStatus:
    reachable: bool
    schema_revision: str | None
    detail: str | None


async def check_database(engine: AsyncEngine) -> DatabaseStatus:
    """Connect, run a trivial query and read the applied Alembic revision."""
    try:
        async with asyncio.timeout(HEALTH_CHECK_TIMEOUT_SECONDS):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                try:
                    revision = (
                        await conn.execute(text("SELECT version_num FROM alembic_version"))
                    ).scalar_one_or_none()
                except ProgrammingError:
                    return DatabaseStatus(
                        reachable=True,
                        schema_revision=None,
                        detail="Database schema not created: run `alembic upgrade head`.",
                    )
    except TimeoutError:
        return DatabaseStatus(False, None, "Database did not respond within 2 seconds.")
    except (DBAPIError, OSError) as exc:
        # Report the failure class only; driver messages can include host details.
        return DatabaseStatus(False, None, f"Database connection failed ({type(exc).__name__}).")
    if revision is None:
        return DatabaseStatus(True, None, "No migration applied: run `alembic upgrade head`.")
    return DatabaseStatus(True, revision, None)
