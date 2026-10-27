"""Repository for strategy configurations and generated signals.

Signals are append-only history: once written, a signal is never updated by
regeneration. Re-running generation for the same strategy config, series and
bar returns the existing row instead of creating a duplicate.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import SignalRecord, StrategyConfigRecord


async def get_or_create_strategy_config(
    session: AsyncSession,
    *,
    name: str,
    version: str,
    parameters: dict[str, Any],
    parameters_hash: str,
) -> int:
    stmt = (
        insert(StrategyConfigRecord)
        .values(name=name, version=version, parameters=parameters, parameters_hash=parameters_hash)
        .on_conflict_do_nothing(constraint="uq_strategy_configs_hash")
        .returning(StrategyConfigRecord.id)
    )
    config_id = (await session.execute(stmt)).scalar_one_or_none()
    if config_id is None:
        config_id = await session.scalar(
            select(StrategyConfigRecord.id).where(
                StrategyConfigRecord.parameters_hash == parameters_hash
            )
        )
    if config_id is None:  # pragma: no cover - would mean a concurrent delete
        raise RuntimeError("strategy config vanished after insert conflict")
    return config_id


@dataclass(frozen=True)
class SaveResult:
    record: SignalRecord
    created: bool


async def save_signal(session: AsyncSession, values: dict[str, Any]) -> SaveResult:
    """Insert a signal, or return the existing one for the same natural key."""
    stmt = (
        insert(SignalRecord)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_signals_key")
        .returning(SignalRecord.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is not None:
        return SaveResult(await session.get_one(SignalRecord, new_id), created=True)
    existing = await session.scalar(
        select(SignalRecord).where(
            SignalRecord.strategy_config_id == values["strategy_config_id"],
            SignalRecord.symbol == values["symbol"],
            SignalRecord.timeframe == values["timeframe"],
            SignalRecord.feed == values["feed"],
            SignalRecord.adjustment == values["adjustment"],
            SignalRecord.signal_bar_at == values["signal_bar_at"],
        )
    )
    if existing is None:  # pragma: no cover - would mean a concurrent delete
        raise RuntimeError("signal vanished after insert conflict")
    return SaveResult(existing, created=False)


async def list_signals(
    session: AsyncSession, *, symbol: str | None = None, limit: int = 50
) -> list[tuple[SignalRecord, StrategyConfigRecord]]:
    """Most recent signal bars first, each with the strategy config that produced it."""
    stmt = (
        select(SignalRecord, StrategyConfigRecord)
        .join(StrategyConfigRecord, SignalRecord.strategy_config_id == StrategyConfigRecord.id)
        .order_by(SignalRecord.signal_bar_at.desc(), SignalRecord.id.desc())
        .limit(limit)
    )
    if symbol is not None:
        stmt = stmt.where(SignalRecord.symbol == symbol)
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]
