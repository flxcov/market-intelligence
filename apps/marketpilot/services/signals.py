"""Generate strategy signals from STORED bars and record them.

The strategy itself is pure; this service supplies it with data from the
database and persists the outcome:
  - bars are read from storage only (no provider calls), up to `as_of`;
  - the exact strategy config (name, version, parameters) is stored once and
    referenced by every signal, so each signal can be traced to its rules;
  - only SIGNAL outcomes are stored. Every evaluation (including "no signal"
    and data-quality refusals) is returned to the caller with its rule checks.
"""

import logging
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.strategies.base import Outcome, StrategyEvaluation
from apps.marketpilot.strategies.trend_following import (
    TrendFollowingParams,
    TrendFollowingStrategy,
)
from packages.database import signals as signal_repo
from packages.database.market_bars import SeriesKey, get_latest_bars
from packages.market_data.models import Adjustment, Bar, DataFeed, Symbol
from packages.shared.timeutils import ensure_utc, utc_now

logger = logging.getLogger(__name__)

MAX_SYMBOLS_PER_REQUEST = 50
# Extra rows loaded beyond what the strategy needs, in case the newest stored
# bars are not yet complete at `as_of` and get filtered out.
_LOAD_MARGIN_BARS = 5


def _dedupe(symbols: list[str]) -> list[str]:
    return list(dict.fromkeys(symbols))


class SignalGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["trend_following"] = "trend_following"
    symbols: Annotated[
        list[Symbol],
        Field(min_length=1, max_length=MAX_SYMBOLS_PER_REQUEST),
        AfterValidator(_dedupe),
    ]
    timeframe: Literal["1Day"] = "1Day"
    feed: DataFeed | None = Field(default=None, description="Defaults to the configured feed.")
    adjustment: Adjustment = Field(
        default=Adjustment.SPLIT,
        description="Must be split- or fully-adjusted: raw prices break moving averages at splits.",
    )
    as_of: AwareDatetime | None = Field(
        default=None, description="Evaluate as of this moment (default: now). Must not be future."
    )
    parameters: TrendFollowingParams = Field(default_factory=TrendFollowingParams)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.adjustment is Adjustment.RAW:
            raise ValueError(
                "adjustment 'raw' is not allowed for strategies: a split would look like a "
                "price crash. Ingest and use 'split' or 'all' adjusted bars."
            )
        if self.as_of is not None:
            self.as_of = ensure_utc(self.as_of)
            if self.as_of > utc_now():
                raise ValueError("as_of must not be in the future")
        return self


class SymbolSignalResult(BaseModel):
    evaluation: StrategyEvaluation
    signal_id: int | None = None
    already_existed: bool = False
    data_retrieved_at: datetime | None = None
    notices: list[str] = Field(default_factory=list)


class SignalGenerationResult(BaseModel):
    strategy: str
    version: str
    parameters_hash: str
    parameters: dict
    feed: DataFeed
    adjustment: Adjustment
    as_of: datetime
    results: list[SymbolSignalResult]


class SignalService:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, default_feed: DataFeed
    ) -> None:
        self._sessions = session_factory
        self._default_feed = default_feed

    async def generate(self, request: SignalGenerationRequest) -> SignalGenerationResult:
        strategy = TrendFollowingStrategy(request.parameters)
        as_of = request.as_of or utc_now()
        feed = request.feed or self._default_feed

        async with self._sessions.begin() as session:
            config_id = await signal_repo.get_or_create_strategy_config(
                session,
                name=strategy.name,
                version=strategy.version,
                parameters=strategy.parameters(),
                parameters_hash=strategy.parameters_hash(),
            )

        results = []
        for symbol in request.symbols:
            key = SeriesKey(symbol, request.timeframe, feed, request.adjustment)
            results.append(await self._generate_one(strategy, config_id, key, as_of))

        return SignalGenerationResult(
            strategy=strategy.name,
            version=strategy.version,
            parameters_hash=strategy.parameters_hash(),
            parameters=strategy.parameters(),
            feed=feed,
            adjustment=request.adjustment,
            as_of=as_of,
            results=results,
        )

    async def _generate_one(
        self, strategy: TrendFollowingStrategy, config_id: int, key: SeriesKey, as_of: datetime
    ) -> SymbolSignalResult:
        async with self._sessions() as session:
            records = await get_latest_bars(
                session, key, end=as_of, limit=strategy.required_bars + _LOAD_MARGIN_BARS
            )
        bars = [Bar.model_validate(r, from_attributes=True) for r in records]
        evaluation = strategy.evaluate(key.symbol, key.timeframe, bars, as_of=as_of)

        used_from = evaluation.last_completed_bar
        retrieved = [
            r.retrieved_at for r in records if used_from is not None and r.timestamp <= used_from
        ]
        data_retrieved_at = max(retrieved) if retrieved else None
        result = SymbolSignalResult(evaluation=evaluation, data_retrieved_at=data_retrieved_at)
        if not records:
            result.notices.append(
                f"No stored {key.adjustment.value}-adjusted {key.feed.value} bars for "
                f"{key.symbol}. Ingest them first with POST /market/bars/ingest."
            )
        if evaluation.outcome is not Outcome.SIGNAL or evaluation.proposal is None:
            return result

        proposal = evaluation.proposal
        values = {
            "strategy_config_id": config_id,
            "symbol": key.symbol,
            "timeframe": key.timeframe,
            "feed": key.feed.value,
            "adjustment": key.adjustment.value,
            "side": proposal.side,
            "status": "new",
            "signal_bar_at": evaluation.last_completed_bar,
            "evaluated_at": as_of,
            "data_retrieved_at": data_retrieved_at,
            "reference_price": proposal.entry.reference_price,
            "stop_price": proposal.exit.initial_stop,
            "cancel_if_open_below": proposal.entry.cancel_if_open_below,
            "cancel_if_open_above": proposal.entry.cancel_if_open_above,
            "expires_after_bars": proposal.entry.expires_after_bars,
            "trailing_exit_lookback_bars": proposal.exit.trailing_exit_lookback_bars,
            "time_stop_bars": proposal.exit.time_stop_bars,
            "atr": proposal.atr,
            "risk_per_share": proposal.risk_per_share,
            "score_components": {
                k: (str(v) if v is not None else None) for k, v in proposal.score_components.items()
            },
            "rule_checks": [c.model_dump() for c in evaluation.checks],
        }
        async with self._sessions.begin() as session:
            saved = await signal_repo.save_signal(session, values)

        result.signal_id = saved.record.id
        result.already_existed = not saved.created
        if not saved.created and (
            saved.record.reference_price != proposal.entry.reference_price
            or saved.record.stop_price != proposal.exit.initial_stop
        ):
            result.notices.append(
                "A signal for this bar already exists with different values (the stored bars "
                "were probably revised since). The original signal is kept unchanged."
            )
        logger.info(
            "signal recorded",
            extra={
                "signal_id": saved.record.id,
                "symbol": key.symbol,
                "strategy": strategy.name,
                "version": strategy.version,
                "newly_created": saved.created,
            },
        )
        return result
