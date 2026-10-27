"""Forward PAPER trading: run a strategy deployment one daily cycle at a time.

A cycle, run once per day after the previous session's bar is complete
(after midnight U.S. Eastern) and before the next open, does:

  1. ingest   new raw + signal-adjusted daily bars (failures are recorded and the
              cycle continues on stored data; stale data is refused downstream);
  2. process  the PAPER account: fills, protective stops, expiries;
  3. exits    strategy exit rules for positions this deployment opened
              (same `exit_reason` the backtester uses);
  4. entries  (active deployments only) signals -> risk-gated signal orders.

Restart safety: every step is idempotent (bar upserts, the account's
`processed_through`, unique signals, one order per signal, exit orders keyed by
deployment/reason/symbol/bar date), so re-running a crashed or repeated cycle
never duplicates anything. A PostgreSQL advisory lock prevents overlapping
cycles for the same deployment and is released if the process dies.

Status: active = full cycle; paused = no new entries, exits still managed;
stopped = fills only (emergency shutdown; terminal).
"""

import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.execution.models import OrderSide, OrderStatus, OrderType, TimeInForce
from apps.marketpilot.services.ingestion import BarIngestionService
from apps.marketpilot.services.option_trading import OptionTradingService
from apps.marketpilot.services.paper_models import (
    ManualOrderRequest,
    OptionOrderRequest,
    SignalOrderRequest,
)
from apps.marketpilot.services.paper_trading import PaperNotFoundError, PaperTradingService
from apps.marketpilot.services.signals import SignalGenerationRequest, SignalService
from apps.marketpilot.strategies.base import Outcome
from apps.marketpilot.strategies.trend_following import (
    TrendFollowingParams,
    TrendFollowingStrategy,
)
from packages.database.market_bars import SeriesKey, get_bars_after, get_latest_bars
from packages.database.models import (
    PaperAccountRecord,
    PaperDeploymentRecord,
    PaperOrderRecord,
    TradingCycleRecord,
)
from packages.market_data.models import Adjustment, BarsRequest, DataFeed, Symbol
from packages.market_data.options import CONTRACT_MULTIPLIER
from packages.market_data.provider import MarketDataProvider
from packages.shared.errors import AppError
from packages.shared.timeutils import utc_now

logger = logging.getLogger(__name__)

DAILY = timedelta(days=1)
_LOCK_NAMESPACE = 72_001  # first key of pg_try_advisory_lock(int, int)
_REINGEST_OVERLAP = timedelta(days=5)  # re-fetch recent bars to pick up provider revisions
MAX_SYMBOLS = 50


class DeploymentConflictError(AppError):
    http_status = 409
    error_type = "conflict"


class DeploymentRequestError(AppError):
    http_status = 400
    error_type = "invalid_deployment_request"


def _dedupe(symbols: list[str]) -> list[str]:
    return list(dict.fromkeys(symbols))


class CreateDeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int = Field(ge=1)
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    strategy: Literal["trend_following"] = "trend_following"
    symbols: Annotated[
        list[Symbol], Field(min_length=1, max_length=MAX_SYMBOLS), AfterValidator(_dedupe)
    ]
    parameters: TrendFollowingParams = Field(default_factory=TrendFollowingParams)
    signal_adjustment: Literal["split", "all"] = "split"


class RunCycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: AwareDatetime | None = Field(default=None, description="Default: now.")
    ingest: bool = Field(default=True, description="Fetch new bars from the provider first.")


class ShutdownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    liquidate: bool = Field(
        default=False, description="Also submit market sells for every open position."
    )


class DeploymentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    mode: Literal["PAPER"] = "PAPER"
    id: int
    account_id: int
    name: str
    strategy: str
    strategy_version: str
    parameters: dict
    parameters_hash: str
    symbols: list[str]
    signal_adjustment: str
    status: str
    last_cycle_at: datetime | None
    created_at: datetime


class CycleView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    mode: Literal["PAPER"] = "PAPER"
    id: int
    deployment_id: int
    as_of: datetime
    status: str
    started_at: datetime
    finished_at: datetime | None
    summary: dict
    error_type: str | None
    error_message: str | None


class ForwardTradingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        default_feed: DataFeed,
        provider: MarketDataProvider | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._default_feed = default_feed
        self._provider = provider
        self._now = now
        self._paper = PaperTradingService(session_factory, default_feed=default_feed, now=now)
        self._options = OptionTradingService(session_factory, now=now)

    # --- deployments ---------------------------------------------------------------------

    async def create(self, request: CreateDeploymentRequest) -> DeploymentView:
        strategy = TrendFollowingStrategy(request.parameters)
        try:
            async with self._sessions.begin() as session:
                account = await session.get(PaperAccountRecord, request.account_id)
                if account is None:
                    raise PaperNotFoundError(f"paper account {request.account_id} not found")
                deployment = PaperDeploymentRecord(
                    account_id=account.id,
                    name=request.name,
                    strategy=strategy.name,
                    strategy_version=strategy.version,
                    parameters=strategy.parameters(),
                    parameters_hash=strategy.parameters_hash(),
                    symbols=request.symbols,
                    signal_adjustment=request.signal_adjustment,
                    status="active",
                    created_at=self._now(),
                )
                session.add(deployment)
                await session.flush()
        except IntegrityError:
            raise DeploymentConflictError(f"a deployment named {request.name!r} exists") from None
        return DeploymentView.model_validate(deployment)

    async def list_deployments(self) -> list[DeploymentView]:
        async with self._sessions() as session:
            rows = (await session.scalars(select(PaperDeploymentRecord).order_by("id"))).all()
        return [DeploymentView.model_validate(r) for r in rows]

    async def get(self, deployment_id: int) -> DeploymentView:
        async with self._sessions() as session:
            return DeploymentView.model_validate(await _get(session, deployment_id))

    async def cycles(self, deployment_id: int, *, limit: int) -> list[CycleView]:
        async with self._sessions() as session:
            await _get(session, deployment_id)
            rows = (
                await session.scalars(
                    select(TradingCycleRecord)
                    .where(TradingCycleRecord.deployment_id == deployment_id)
                    .order_by(TradingCycleRecord.id.desc())
                    .limit(limit)
                )
            ).all()
        return [CycleView.model_validate(r) for r in rows]

    async def set_status(
        self, deployment_id: int, status: Literal["active", "paused"]
    ) -> DeploymentView:
        async with self._sessions.begin() as session:
            deployment = await _get(session, deployment_id, for_update=True)
            if deployment.status == "stopped":
                raise DeploymentConflictError("a stopped deployment cannot be restarted")
            deployment.status = status
            deployment.updated_at = self._now()
        return await self.get(deployment_id)

    async def shutdown(self, deployment_id: int, request: ShutdownRequest) -> dict[str, Any]:
        """Emergency stop: halt the account (cancels working buys), stop the deployment,
        and optionally submit market sells for every open position."""
        async with self._sessions.begin() as session:
            deployment = await _get(session, deployment_id, for_update=True)
            deployment.status = "stopped"
            deployment.updated_at = self._now()
            account_id = deployment.account_id
        view = await self._paper.set_halted(account_id, True)
        liquidation = []
        if request.liquidate:
            for position in view.positions:
                if not position.quantity:
                    continue
                client_id = f"d{deployment_id}-liquidate-{position.symbol}-{self._now().date()}"
                if position.multiplier == CONTRACT_MULTIPLIER:
                    # Option positions close at the bid of a later quote (market order, GTC
                    # so it can still fill after today's close if quotes are ingested).
                    result = await self._options.submit_order(
                        account_id,
                        OptionOrderRequest(
                            symbol=position.symbol,
                            side=OrderSide.SELL,
                            order_type=OrderType.MARKET,
                            time_in_force=TimeInForce.GTC,
                            client_order_id=client_id,
                        ),
                        deployment_id=deployment_id,
                    )
                else:
                    result = await self._paper.submit_order(
                        account_id, _sell(position.symbol, client_id), deployment_id=deployment_id
                    )
                liquidation.append(_order_summary(position.symbol, result))
        logger.warning(
            "deployment shut down",
            extra={"deployment_id": deployment_id, "liquidation_orders": len(liquidation)},
        )
        return {
            "mode": "PAPER",
            "deployment": (await self.get(deployment_id)).model_dump(mode="json"),
            "account_status": "halted",
            "liquidation_orders": liquidation,
        }

    # --- the daily cycle -------------------------------------------------------------------

    async def run_cycle(self, deployment_id: int, request: RunCycleRequest) -> CycleView:
        now = self._now()
        as_of = request.as_of or now
        if as_of > now:
            raise DeploymentRequestError("as_of must not be in the future")

        async with self._sessions() as lock_session:
            locked = await lock_session.scalar(
                text("SELECT pg_try_advisory_lock(:ns, :id)"),
                {"ns": _LOCK_NAMESPACE, "id": deployment_id},
            )
            if not locked:
                raise DeploymentConflictError(
                    f"a cycle for deployment {deployment_id} is already running"
                )
            try:
                return await self._locked_cycle(deployment_id, as_of, request.ingest)
            finally:
                await lock_session.execute(
                    text("SELECT pg_advisory_unlock(:ns, :id)"),
                    {"ns": _LOCK_NAMESPACE, "id": deployment_id},
                )

    async def _locked_cycle(self, deployment_id: int, as_of: datetime, ingest: bool) -> CycleView:
        async with self._sessions.begin() as session:
            deployment = await _get(session, deployment_id)
            # A cycle still marked running can only be left over from a crash.
            await session.execute(
                update(TradingCycleRecord)
                .where(
                    TradingCycleRecord.deployment_id == deployment_id,
                    TradingCycleRecord.status == "running",
                )
                .values(
                    status="interrupted",
                    finished_at=self._now(),
                    error_message="did not finish (process stopped or crashed)",
                )
            )
            cycle = TradingCycleRecord(
                deployment_id=deployment_id,
                as_of=as_of,
                status="running",
                started_at=self._now(),
                summary={"deployment_status": deployment.status},
            )
            session.add(cycle)
            await session.flush()
            cycle_id = cycle.id
            account = await session.get_one(PaperAccountRecord, deployment.account_id)
            feed = DataFeed(account.feed)

        summary: dict[str, Any] = {"deployment_status": deployment.status}
        status, error_type, error_message = "succeeded", None, None
        try:
            if ingest:
                summary["ingestion"] = await self._ingest(deployment, feed, as_of)
                if any("error" in item for item in summary["ingestion"]):
                    status = "partial"
            processed = await self._paper.process(deployment.account_id, as_of)
            summary["processing"] = {
                "sessions": [s.isoformat() for s in processed.sessions_processed],
                "events": [e.model_dump(mode="json") for e in processed.events],
                "notices": processed.notices,
            }
            if deployment.status in ("active", "paused"):
                summary["exits"] = await self._exits(deployment, feed, as_of)
            if deployment.status == "active":
                summary.update(await self._entries(deployment, feed, as_of))
            view = await self._paper.account_view(deployment.account_id)
            summary["account"] = {
                "cash": str(view.cash),
                "equity": str(view.equity),
                "open_positions": sum(1 for p in view.positions if p.quantity),
                "drawdown_pct": str(view.drawdown_pct),
            }
        except Exception as exc:
            status = "failed"
            error_type = exc.error_type if isinstance(exc, AppError) else "internal_error"
            error_message = exc.message if isinstance(exc, AppError) else type(exc).__name__
            logger.exception("trading cycle failed", extra={"deployment_id": deployment_id})

        async with self._sessions.begin() as session:
            cycle = await session.get_one(TradingCycleRecord, cycle_id)
            cycle.status = status
            cycle.finished_at = self._now()
            cycle.summary = summary
            cycle.error_type = error_type
            cycle.error_message = error_message
            record = await session.get_one(PaperDeploymentRecord, deployment_id)
            record.last_cycle_at = self._now()
        logger.info(
            "trading cycle finished",
            extra={"deployment_id": deployment_id, "cycle_id": cycle_id, "status": status},
        )
        async with self._sessions() as session:
            return CycleView.model_validate(await session.get_one(TradingCycleRecord, cycle_id))

    async def _ingest(
        self, deployment: PaperDeploymentRecord, feed: DataFeed, as_of: datetime
    ) -> list[dict[str, Any]]:
        if self._provider is None:
            return [{"error": "market data provider not configured; using stored bars only"}]
        service = BarIngestionService(self._provider, self._sessions)
        strategy = TrendFollowingStrategy(TrendFollowingParams(**deployment.parameters))
        warmup = timedelta(days=math.ceil(strategy.required_bars * 1.5) + 10)
        results = []
        for symbol in deployment.symbols:
            for adjustment in (Adjustment.RAW, Adjustment(deployment.signal_adjustment)):
                async with self._sessions() as session:
                    latest = await get_latest_bars(
                        session, SeriesKey(symbol, "1Day", feed, adjustment), end=as_of, limit=1
                    )
                start = latest[-1].timestamp - _REINGEST_OVERLAP if latest else as_of - warmup
                item: dict[str, Any] = {"symbol": symbol, "adjustment": adjustment.value}
                try:
                    result = await service.ingest(
                        BarsRequest(
                            symbol=symbol,
                            timeframe="1Day",
                            start=start,
                            end=as_of,
                            adjustment=adjustment,
                            feed=feed,
                        )
                    )
                    item.update(
                        inserted=result.inserted, updated=result.updated, unchanged=result.unchanged
                    )
                except AppError as exc:
                    item["error"] = f"{exc.error_type}: {exc.message}"
                results.append(item)
        return results

    async def _exits(
        self, deployment: PaperDeploymentRecord, feed: DataFeed, as_of: datetime
    ) -> list[dict[str, Any]]:
        strategy = TrendFollowingStrategy(TrendFollowingParams(**deployment.parameters))
        view = await self._paper.account_view(deployment.account_id)
        held = {p.symbol for p in view.positions if p.quantity}
        exits = []
        async with self._sessions() as session:
            working_sells = set(
                (
                    await session.scalars(
                        select(PaperOrderRecord.symbol).where(
                            PaperOrderRecord.account_id == deployment.account_id,
                            PaperOrderRecord.status == OrderStatus.WORKING,
                            PaperOrderRecord.side == OrderSide.SELL,
                            PaperOrderRecord.order_type != OrderType.STOP,
                        )
                    )
                ).all()
            )
            candidates = []
            for symbol in sorted(held & set(deployment.symbols)):
                entry = await session.scalar(
                    select(PaperOrderRecord)
                    .where(
                        PaperOrderRecord.account_id == deployment.account_id,
                        PaperOrderRecord.deployment_id == deployment.id,
                        PaperOrderRecord.symbol == symbol,
                        PaperOrderRecord.side == OrderSide.BUY,
                        PaperOrderRecord.status == OrderStatus.FILLED,
                    )
                    .order_by(PaperOrderRecord.filled_bar_at.desc())
                    .limit(1)
                )
                if entry is None or entry.filled_bar_at is None or symbol in working_sells:
                    continue  # not opened by this deployment, or an exit is already working
                key = SeriesKey(symbol, "1Day", feed, Adjustment.RAW)
                history = await get_latest_bars(
                    session, key, end=as_of - DAILY, limit=strategy.params.exit_lookback + 1
                )
                if not history or history[-1].timestamp < entry.filled_bar_at:
                    continue
                since_entry = await get_bars_after(
                    session, key, after=entry.filled_bar_at, through=history[-1].timestamp
                )
                reason = strategy.exit_reason(history, len(since_entry))
                if reason is not None:
                    candidates.append((symbol, reason, history[-1].timestamp))
        for symbol, reason, bar_at in candidates:
            client_id = f"d{deployment.id}-{reason}-{symbol}-{bar_at.date()}"
            result = await self._paper.submit_order(
                deployment.account_id, _sell(symbol, client_id), deployment_id=deployment.id
            )
            exits.append({**_order_summary(symbol, result), "exit_reason": reason})
        return exits

    async def _entries(
        self, deployment: PaperDeploymentRecord, feed: DataFeed, as_of: datetime
    ) -> dict[str, Any]:
        generated = await SignalService(self._sessions, default_feed=feed).generate(
            SignalGenerationRequest(
                symbols=deployment.symbols,
                feed=feed,
                adjustment=deployment.signal_adjustment,
                as_of=as_of,
                parameters=TrendFollowingParams(**deployment.parameters),
            )
        )
        view = await self._paper.account_view(deployment.account_id)
        held = {p.symbol for p in view.positions if p.quantity}
        working = await self._paper.list_orders(
            deployment.account_id, status=OrderStatus.WORKING, limit=1000
        )
        pending = {o.symbol for o in working if o.side == OrderSide.BUY}
        entries, skipped = [], []
        for item in generated.results:
            symbol = item.evaluation.symbol
            if item.evaluation.outcome is not Outcome.SIGNAL or item.signal_id is None:
                continue
            if symbol in held or symbol in pending:
                skipped.append({"symbol": symbol, "reason": "already held or pending"})
                continue
            result = await self._paper.submit_signal_order(
                deployment.account_id,
                SignalOrderRequest(signal_id=item.signal_id),
                deployment_id=deployment.id,
            )
            entries.append({**_order_summary(symbol, result), "signal_id": item.signal_id})
        return {
            "signals": {r.evaluation.symbol: r.evaluation.outcome.value for r in generated.results},
            "entries": entries,
            "skipped_entries": skipped,
        }


async def _get(
    session: AsyncSession, deployment_id: int, *, for_update: bool = False
) -> PaperDeploymentRecord:
    stmt = select(PaperDeploymentRecord).where(PaperDeploymentRecord.id == deployment_id)
    if for_update:
        stmt = stmt.with_for_update()
    deployment = await session.scalar(stmt)
    if deployment is None:
        raise PaperNotFoundError(f"deployment {deployment_id} not found")
    return deployment


def _sell(symbol: str, client_order_id: str) -> ManualOrderRequest:
    return ManualOrderRequest(
        symbol=symbol,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,  # keep trying at each open until filled
        client_order_id=client_order_id,
    )


def _order_summary(symbol: str, result) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "order_id": result.order.id,
        "accepted": result.accepted,
        "already_existed": result.already_existed,
        "status": result.order.status.value,
        "reason": result.order.status_reason,
        "quantity": result.order.quantity,
    }
