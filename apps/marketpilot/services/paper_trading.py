"""PAPER trading service: accounts, orders, the bar-by-bar simulation and reconciliation.

Nothing here talks to a broker. Orders are filled by `fill_simulator` against
STORED raw daily bars, accounted for by `Portfolio`, and gated by the risk
module, which runs both when an order is submitted AND again at the moment it
would fill (with the actual fill price and the account's state at that time).

Simulation clock: `process()` walks forward through daily sessions that have
completed by `as_of` and that the account has not processed yet. Each session
is handled once; `processed_through` records how far the account has got.
Bars ingested later for an already-processed session are NOT replayed.

Options (long calls/puts) share the same account, ledger and risk limits; their
fills come from stored option QUOTES, not from bars (see `option_trading`).

Sources of truth: the ledger (`paper_ledger`) for cash and positions; the
`cash` column and `paper_positions` table are caches checked by `reconcile()`.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.execution.costs import CostModel
from apps.marketpilot.execution.fill_simulator import (
    UnsupportedBarTimestampError,
    session_open,
    simulate_on_bar,
)
from apps.marketpilot.execution.models import (
    OrderSide,
    OrderSpec,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from apps.marketpilot.execution.options import OptionSettings
from apps.marketpilot.execution.portfolio import (
    AccountingError,
    LedgerKind,
    Portfolio,
)
from apps.marketpilot.risk.limits import (
    AccountRiskState,
    EntryIntent,
    RiskDecision,
    RiskLimits,
    check_entry,
    check_exit,
)
from apps.marketpilot.risk.position_sizing import size_entry
from apps.marketpilot.services.option_trading import OPTION, OptionSimulation
from apps.marketpilot.services.paper_common import (
    DAILY,
    TIMEFRAME,
    PaperConflictError,
    PaperNotFoundError,
    PaperRequestError,
    add_ledger,
    close_order,
    current_marks,
    finish_submission,
    get_account,
    held_symbols,
    load_equity_history,
    load_portfolio,
    option_contract,
    option_settings,
    order_result,
    raw_key,
    risk_state,
    save_positions,
)
from apps.marketpilot.services.paper_models import (
    AccountSummary,
    AccountView,
    CreateAccountRequest,
    EquitySnapshotView,
    FillView,
    LedgerEntryView,
    ManualOrderRequest,
    OrderResult,
    OrderView,
    PositionView,
    ProcessEvent,
    ProcessResult,
    ReconcileResult,
    SignalOrderRequest,
)
from apps.marketpilot.strategies.base import check
from packages.database.market_bars import get_bars_after, get_latest_bars
from packages.database.models import (
    MarketBarRecord,
    PaperAccountRecord,
    PaperEquitySnapshotRecord,
    PaperFillRecord,
    PaperLedgerRecord,
    PaperOrderRecord,
    PaperPositionRecord,
    SignalRecord,
)
from packages.database.option_quotes import latest_quote
from packages.market_data.models import Adjustment, DataFeed
from packages.market_data.options import OptionFeed
from packages.shared.timeutils import utc_now

logger = logging.getLogger(__name__)

__all__ = [
    "PaperConflictError",
    "PaperNotFoundError",
    "PaperRequestError",
    "PaperTradingService",
]

_HUNDRED = Decimal(100)
_TICK = timedelta(microseconds=1)


class PaperTradingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        default_feed: DataFeed,
        default_option_feed: OptionFeed = OptionFeed.INDICATIVE,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._default_feed = default_feed
        self._default_option_feed = default_option_feed
        self._now = now

    # --- accounts ---------------------------------------------------------------------

    async def create_account(self, request: CreateAccountRequest) -> AccountView:
        now = self._now()
        try:
            async with self._sessions.begin() as session:
                account = PaperAccountRecord(
                    name=request.name,
                    status="active",
                    feed=(request.feed or self._default_feed).value,
                    starting_cash=request.starting_cash,
                    cash=request.starting_cash,
                    risk_limits=request.risk_limits.model_dump(mode="json"),
                    cost_model=request.cost_model.model_dump(mode="json"),
                    option_settings=(
                        request.option_settings
                        or OptionSettings(quote_feed=self._default_option_feed)
                    ).model_dump(mode="json"),
                    # Same clock as orders, so the simulation never looks before creation.
                    created_at=now,
                )
                session.add(account)
                await session.flush()
                lines = Portfolio().deposit(request.starting_cash, now)
                await add_ledger(session, account.id, lines)
        except IntegrityError:
            raise PaperConflictError(f"a paper account named {request.name!r} exists") from None
        logger.info("paper account created", extra={"account_id": account.id})
        return await self.account_view(account.id)

    async def list_accounts(self) -> list[AccountSummary]:
        async with self._sessions() as session:
            rows = (await session.scalars(select(PaperAccountRecord).order_by("id"))).all()
        return [_summary(a) for a in rows]

    async def account_view(self, account_id: int) -> AccountView:
        now = self._now()
        async with self._sessions() as session:
            account = await get_account(session, account_id)
            portfolio = await load_portfolio(session, account_id)
            held = held_symbols(portfolio)
            marks, mark_times, notices = await current_marks(session, account, held, as_of=now)
            valuation = portfolio.valuation(marks)
            history = await load_equity_history(session, account)
            working = await session.scalar(
                select(func.count())
                .select_from(PaperOrderRecord)
                .where(
                    PaperOrderRecord.account_id == account_id,
                    PaperOrderRecord.status == OrderStatus.WORKING,
                )
            )
        peak = history.peak(valuation.equity)
        return AccountView(
            **_summary(account).model_dump(exclude={"mode"}),
            valued_at=now,
            equity=valuation.equity,
            market_value=valuation.market_value,
            realized_pnl=valuation.realized_pnl,
            unrealized_pnl=valuation.unrealized_pnl,
            fees=valuation.fees,
            total_return_pct=_pct(valuation.equity - valuation.deposits, valuation.deposits),
            peak_equity=peak,
            drawdown_pct=_pct(peak - valuation.equity, peak),
            positions=[
                PositionView(**vars(p), mark_bar_at=mark_times.get(p.symbol))
                for p in valuation.positions
                if p.quantity or p.realized_pnl
            ],
            working_orders=working or 0,
            risk_limits=RiskLimits(**account.risk_limits),
            cost_model=CostModel(**account.cost_model),
            option_settings=option_settings(account),
            notices=notices,
        )

    async def set_halted(self, account_id: int, halted: bool) -> AccountView:
        """Emergency stop: block new entries and cancel working buys. Stops stay active."""
        async with self._sessions.begin() as session:
            account = await get_account(session, account_id, for_update=True)
            account.status = "halted" if halted else "active"
            account.updated_at = func.now()
            if halted:
                await session.execute(
                    update(PaperOrderRecord)
                    .where(
                        PaperOrderRecord.account_id == account_id,
                        PaperOrderRecord.status == OrderStatus.WORKING,
                        PaperOrderRecord.side == OrderSide.BUY,
                    )
                    .values(
                        status=OrderStatus.CANCELLED,
                        status_reason="account halted",
                        closed_at=self._now(),
                    )
                )
        logger.warning(
            "paper account halted" if halted else "paper account resumed",
            extra={"account_id": account_id},
        )
        return await self.account_view(account_id)

    # --- orders -----------------------------------------------------------------------

    async def submit_order(
        self, account_id: int, request: ManualOrderRequest, *, deployment_id: int | None = None
    ) -> OrderResult:
        now = self._now()
        async with self._sessions.begin() as session:
            account = await get_account(session, account_id, for_update=True)
            if request.client_order_id:
                existing = await session.scalar(
                    select(PaperOrderRecord).where(
                        PaperOrderRecord.account_id == account_id,
                        PaperOrderRecord.client_order_id == request.client_order_id,
                    )
                )
                if existing is not None:
                    return order_result(existing, already_existed=True)

            context = await _OrderContext.load(session, account, request.symbol, as_of=now)
            order = PaperOrderRecord(
                account_id=account_id,
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                time_in_force=request.time_in_force,
                limit_price=request.limit_price,
                stop_price=request.stop_price,
                protective_stop_price=request.protective_stop_price,
                client_order_id=request.client_order_id,
                deployment_id=deployment_id,
                submitted_at=now,
                active_from=now,
            )
            if request.side is OrderSide.BUY:
                assert request.protective_stop_price is not None  # enforced by the request model
                worst_entry = {
                    OrderType.LIMIT: request.limit_price,
                    OrderType.STOP: request.stop_price,
                }.get(request.order_type) or context.last_price
                decision = context.gate_entry(
                    order, request.quantity, worst_entry, request.protective_stop_price
                )
            else:
                quantity = request.quantity or context.state.position_quantities.get(
                    request.symbol, 0
                )
                order.quantity = quantity
                decision = check_exit(request.symbol, quantity, context.state)
            finish_submission(order, decision, now)
            session.add(order)
            await session.flush()
        logger.info(
            "paper order submitted",
            extra={"account_id": account_id, "order_id": order.id, "status": order.status},
        )
        return order_result(order)

    async def submit_signal_order(
        self, account_id: int, request: SignalOrderRequest, *, deployment_id: int | None = None
    ) -> OrderResult:
        now = self._now()
        async with self._sessions.begin() as session:
            account = await get_account(session, account_id, for_update=True)
            signal = await session.get(SignalRecord, request.signal_id)
            if signal is None:
                raise PaperNotFoundError(f"signal {request.signal_id} not found")
            if signal.feed != account.feed:
                raise PaperRequestError(
                    f"signal {signal.id} was computed on the {signal.feed} feed but this "
                    f"account trades on {account.feed}"
                )
            existing = await session.scalar(
                select(PaperOrderRecord).where(
                    PaperOrderRecord.account_id == account_id,
                    PaperOrderRecord.signal_id == signal.id,
                    PaperOrderRecord.status != OrderStatus.REJECTED,
                )
            )
            if existing is not None:
                return order_result(existing, already_existed=True)

            context = await _OrderContext.load(session, account, signal.symbol, as_of=now)
            order = PaperOrderRecord(
                account_id=account_id,
                symbol=signal.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                protective_stop_price=signal.stop_price,
                cancel_if_open_below=signal.cancel_if_open_below,
                cancel_if_open_above=signal.cancel_if_open_above,
                signal_id=signal.id,
                deployment_id=deployment_id,
                submitted_at=now,
                active_from=now,
                only_first_bar_after=signal.signal_bar_at,
            )
            # Size at the worst price the order may fill at: the cancel-above bound.
            decision = context.gate_entry(
                order, None, signal.cancel_if_open_above, signal.stop_price
            )
            entry_bar = await _first_bar_after(
                session, account, signal.symbol, signal.signal_bar_at
            )
            if entry_bar is not None and session_open(entry_bar) < now:
                decision = RiskDecision(
                    False,
                    [
                        *decision.checks,
                        check(
                            "signal_still_valid",
                            False,
                            f"the entry session ({entry_bar.timestamp.date()}) opened before "
                            "this order was submitted",
                        ),
                    ],
                )
            finish_submission(order, decision, now)
            session.add(order)
            await session.flush()
        return order_result(order)

    async def cancel_order(self, account_id: int, order_id: int) -> OrderResult:
        async with self._sessions.begin() as session:
            await get_account(session, account_id, for_update=True)
            order = await session.get(PaperOrderRecord, order_id)
            if order is None or order.account_id != account_id:
                raise PaperNotFoundError(f"order {order_id} not found in account {account_id}")
            if order.status != OrderStatus.WORKING:
                raise PaperConflictError(f"order {order_id} is {order.status}, not working")
            close_order(order, OrderStatus.CANCELLED, "cancelled by user", self._now())
        return order_result(order)

    async def list_orders(
        self, account_id: int, *, status: OrderStatus | None, limit: int
    ) -> list[OrderView]:
        async with self._sessions() as session:
            await get_account(session, account_id)
            stmt = (
                select(PaperOrderRecord)
                .where(PaperOrderRecord.account_id == account_id)
                .order_by(PaperOrderRecord.id.desc())
                .limit(limit)
            )
            if status is not None:
                stmt = stmt.where(PaperOrderRecord.status == status)
            return [OrderView.model_validate(o) for o in (await session.scalars(stmt)).all()]

    async def list_fills(self, account_id: int, *, limit: int) -> list[FillView]:
        async with self._sessions() as session:
            await get_account(session, account_id)
            stmt = (
                select(PaperFillRecord)
                .where(PaperFillRecord.account_id == account_id)
                .order_by(PaperFillRecord.id.desc())
                .limit(limit)
            )
            return [FillView.model_validate(f) for f in (await session.scalars(stmt)).all()]

    async def list_ledger(self, account_id: int, *, limit: int) -> list[LedgerEntryView]:
        async with self._sessions() as session:
            await get_account(session, account_id)
            stmt = (
                select(PaperLedgerRecord)
                .where(PaperLedgerRecord.account_id == account_id)
                .order_by(PaperLedgerRecord.id.desc())
                .limit(limit)
            )
            return [LedgerEntryView.model_validate(r) for r in (await session.scalars(stmt)).all()]

    async def equity_history(self, account_id: int, *, limit: int) -> list[EquitySnapshotView]:
        """Session-close equity snapshots, oldest first (the most recent `limit`)."""
        async with self._sessions() as session:
            await get_account(session, account_id)
            stmt = (
                select(PaperEquitySnapshotRecord)
                .where(PaperEquitySnapshotRecord.account_id == account_id)
                .order_by(PaperEquitySnapshotRecord.bar_at.desc())
                .limit(limit)
            )
            rows = (await session.scalars(stmt)).all()
        return [EquitySnapshotView.model_validate(r) for r in reversed(rows)]

    # --- simulation ---------------------------------------------------------------------

    async def process(self, account_id: int, as_of: datetime | None = None) -> ProcessResult:
        now = self._now()
        as_of = as_of or now
        if as_of > now:
            raise PaperRequestError("as_of must not be in the future")
        async with self._sessions.begin() as session:
            account = await get_account(session, account_id, for_update=True)
            run = _SimulationRun(session, account, now=now)
            await run.execute(as_of)
        if run.events:
            logger.info(
                "paper simulation processed",
                extra={"account_id": account_id, "events": len(run.events)},
            )
        return ProcessResult(
            account_id=account_id,
            as_of=as_of,
            sessions_processed=run.sessions,
            processed_through=account.processed_through,
            events=run.events,
            equity=run.last_equity,
            notices=run.notices,
        )

    # --- reconciliation -----------------------------------------------------------------

    async def reconcile(self, account_id: int) -> ReconcileResult:
        now = self._now()
        async with self._sessions() as session:
            account = await get_account(session, account_id)
            portfolio = await load_portfolio(session, account_id)
            stored = {
                p.symbol: p
                for p in (
                    await session.scalars(
                        select(PaperPositionRecord).where(
                            PaperPositionRecord.account_id == account_id
                        )
                    )
                ).all()
            }
            fills = (
                await session.scalars(
                    select(PaperFillRecord).where(PaperFillRecord.account_id == account_id)
                )
            ).all()
            expiry_cash = await session.scalar(
                select(func.coalesce(func.sum(PaperLedgerRecord.amount), 0)).where(
                    PaperLedgerRecord.account_id == account_id,
                    PaperLedgerRecord.kind == LedgerKind.EXPIRE,
                )
            )
            trade_lines = await session.scalar(
                select(func.count())
                .select_from(PaperLedgerRecord)
                .where(
                    PaperLedgerRecord.account_id == account_id,
                    PaperLedgerRecord.kind.in_([LedgerKind.BUY, LedgerKind.SELL]),
                )
            )
            marks, _, _ = await current_marks(session, account, held_symbols(portfolio), as_of=now)

        mismatched = sorted(
            symbol
            for symbol in set(stored) | set(portfolio.positions)
            if _position_tuple(stored.get(symbol))
            != _position_tuple(portfolio.positions.get(symbol))
        )
        fill_cash = sum(
            (
                (-1 if f.side == OrderSide.BUY else 1) * f.price * f.quantity * f.multiplier
                - f.commission
                for f in fills
            ),
            Decimal(0),
        )
        valuation = portfolio.valuation(marks)
        checks = [
            check(
                "cash_matches_ledger",
                account.cash == portfolio.cash,
                f"stored {account.cash} vs ledger {portfolio.cash}",
            ),
            check(
                "positions_match_ledger",
                not mismatched,
                f"mismatched: {', '.join(mismatched)}" if mismatched else "all positions match",
            ),
            check(
                "fills_match_ledger",
                trade_lines == len(fills)
                and fill_cash + expiry_cash == portfolio.cash - portfolio.deposits,
                f"{len(fills)} fills vs {trade_lines} trade ledger lines; fill cash flow "
                f"{fill_cash} + option expiry settlements {expiry_cash} vs ledger "
                f"{portfolio.cash - portfolio.deposits}",
            ),
            check(
                "accounting_identity",
                valuation.identity_difference == 0,
                "equity - deposits == realized + unrealized - fees "
                f"(difference {valuation.identity_difference})",
            ),
            check("no_negative_cash", portfolio.cash >= 0, f"cash {portfolio.cash}"),
        ]
        return ReconcileResult(
            account_id=account_id, balanced=all(c.passed for c in checks), checks=checks
        )


# --- order submission helpers ---------------------------------------------------------


@dataclass
class _OrderContext:
    """Everything the risk gate needs about the account and the symbol right now."""

    account: PaperAccountRecord
    limits: RiskLimits
    costs: CostModel
    state: AccountRiskState
    last_price: Decimal | None
    average_volume: Decimal | None

    @classmethod
    async def load(
        cls, session: AsyncSession, account: PaperAccountRecord, symbol: str, *, as_of: datetime
    ) -> "_OrderContext":
        limits = RiskLimits(**account.risk_limits)
        portfolio = await load_portfolio(session, account.id)
        marks, _, _ = await current_marks(session, account, held_symbols(portfolio), as_of=as_of)
        working = (
            await session.scalars(
                select(PaperOrderRecord).where(
                    PaperOrderRecord.account_id == account.id,
                    PaperOrderRecord.status == OrderStatus.WORKING,
                )
            )
        ).all()
        state = risk_state(
            account, portfolio, marks, await load_equity_history(session, account), working
        )
        recent = await get_latest_bars(
            session,
            raw_key(account, symbol),
            end=as_of - DAILY,
            limit=limits.avg_volume_lookback,
        )
        return cls(
            account=account,
            limits=limits,
            costs=CostModel(**account.cost_model),
            state=state,
            last_price=recent[-1].close if recent else None,
            average_volume=_mean_volume(recent),
        )

    def gate_entry(
        self,
        order: PaperOrderRecord,
        quantity: int | None,
        worst_entry: Decimal | None,
        stop: Decimal,
    ) -> RiskDecision:
        """Size (if needed) and risk-check a buy. Sets quantity, sizing and reserved cost."""
        if self.last_price is None or worst_entry is None:
            order.quantity = quantity or 0
            return RiskDecision(
                False,
                [check("market_data_available", False, "no completed raw bars for this symbol")],
            )
        if quantity is None:
            sizing = size_entry(
                worst_entry_price=worst_entry,
                protective_stop=stop,
                average_volume=self.average_volume,
                state=self.state,
                limits=self.limits,
                costs=self.costs,
            )
            order.sizing = {
                "quantity": sizing.quantity,
                "limiting_factor": sizing.limiting_factor,
                "candidates": sizing.candidates,
                "worst_entry_price": str(worst_entry),
            }
            quantity = sizing.quantity
        order.quantity = quantity
        if quantity < 1:
            return RiskDecision(
                False,
                [check("position_size", False, "risk limits allow less than one share")],
            )
        order.reserved_cost = self.costs.adverse_price(
            worst_entry, OrderSide.BUY
        ) * quantity + self.costs.commission(quantity)
        intent = EntryIntent(
            order.symbol, quantity, worst_entry, stop, self.last_price, self.average_volume
        )
        return check_entry(intent, self.state, self.limits, self.costs)


# --- the simulation run ----------------------------------------------------------------


class _SimulationRun:
    def __init__(self, session: AsyncSession, account: PaperAccountRecord, *, now: datetime):
        self.session = session
        self.account = account
        self.now = now
        self.limits = RiskLimits(**account.risk_limits)
        self.costs = CostModel(**account.cost_model)
        self.events: list[ProcessEvent] = []
        self.sessions: list[datetime] = []
        self.notices: list[str] = []
        self.last_equity: Decimal | None = None

    async def execute(self, as_of: datetime) -> None:
        session, account = self.session, self.account
        self.portfolio = await load_portfolio(session, account.id)
        self.history = await load_equity_history(session, account)
        self.orders = list(
            (
                await session.scalars(
                    select(PaperOrderRecord)
                    .where(
                        PaperOrderRecord.account_id == account.id,
                        PaperOrderRecord.status == OrderStatus.WORKING,
                    )
                    .order_by(PaperOrderRecord.id)
                )
            ).all()
        )
        self.options = OptionSimulation(self)
        equity_orders = [o for o in self.orders if o.asset_class != OPTION]
        held_shares = {s for s in held_symbols(self.portfolio) if option_contract(s) is None}
        symbols = sorted(
            {o.symbol for o in equity_orders} | held_shares | self.options.underlyings()
        )
        if not symbols:
            self.notices.append("Nothing to process: no working orders or open positions.")
            return

        processed = account.processed_through
        lower = min(
            [processed or account.created_at - DAILY]
            + [o.only_first_bar_after for o in self.orders if o.only_first_bar_after]
        )
        completed_through = as_of - DAILY  # a daily bar is complete one day after its start
        self.bars: dict[str, list[MarketBarRecord]] = {}
        for symbol in symbols:
            self.bars[symbol] = await get_bars_after(
                session, raw_key(account, symbol), after=lower, through=completed_through
            )
        new_sessions = sorted(
            {
                bar.timestamp
                for bars in self.bars.values()
                for bar in bars
                if processed is None or bar.timestamp > processed
            }
        )
        for ts in new_sessions:
            await self._process_session(ts, symbols)
            self.sessions.append(ts)
        # Option quotes after the last completed session, up to and including as_of.
        await self.options.step(through=as_of + _TICK)

        for order in equity_orders:
            if order.status == OrderStatus.WORKING and not self.bars.get(order.symbol):
                self.notices.append(
                    f"Order {order.id} ({order.symbol}): no new completed raw "
                    f"{account.feed} bars stored. Ingest them, then process again."
                )
        if new_sessions:
            account.processed_through = new_sessions[-1]
        account.cash = self.portfolio.cash
        account.updated_at = func.now()
        await save_positions(session, account.id, self.portfolio)

    async def _process_session(self, ts: datetime, symbols: Sequence[str]) -> None:
        # Equity at the previous close: the base for risk limits during this session.
        # Option quotes count up to the end of the bar's calendar day.
        self.equity_before = (await self._valuation(ts - _TICK, option_cutoff=ts)).equity
        for symbol in symbols:
            bar = next((b for b in self.bars[symbol] if b.timestamp == ts), None)
            if bar is not None:
                await self._process_bar(symbol, bar)
        # Then that day's option quotes and any expiry settled by this close.
        await self.options.step(through=ts + DAILY)
        valuation = await self._valuation(ts, option_cutoff=ts + DAILY)
        self.history.equities.append(valuation.equity)
        self.last_equity = valuation.equity
        await self.session.execute(
            insert(PaperEquitySnapshotRecord)
            .values(
                account_id=self.account.id,
                bar_at=ts,
                cash=valuation.cash,
                market_value=valuation.market_value,
                equity=valuation.equity,
            )
            .on_conflict_do_nothing(constraint="uq_paper_equity_snapshots_bar")
        )

    async def _process_bar(self, symbol: str, bar: MarketBarRecord) -> None:
        try:
            opens_at = session_open(bar)
        except UnsupportedBarTimestampError as exc:
            raise PaperConflictError(str(exc)) from None
        # Orders that execute at the open (market) go before stops/limits, which can
        # only trigger later in the session.
        queue = sorted(
            (o for o in self.orders if o.symbol == symbol and o.asset_class != OPTION),
            key=lambda o: (o.order_type != OrderType.MARKET, o.id),
        )
        index = 0
        while index < len(queue):
            order = queue[index]
            index += 1
            if order.status != OrderStatus.WORKING:
                continue
            if order.only_first_bar_after is not None:
                first = next(
                    (b for b in self.bars[symbol] if b.timestamp > order.only_first_bar_after),
                    None,
                )
                if first is None or bar.timestamp < first.timestamp:
                    continue
                if bar.timestamp > first.timestamp:
                    self._finish(order, OrderStatus.EXPIRED, bar, "entry session has passed")
                    continue
                if opens_at < order.active_from:
                    self._finish(
                        order,
                        OrderStatus.EXPIRED,
                        bar,
                        "submitted after the entry session had opened",
                    )
                    continue
            elif opens_at < order.active_from:
                continue  # this session opened before the order existed

            spec = OrderSpec(
                side=OrderSide(order.side),
                order_type=OrderType(order.order_type),
                quantity=order.quantity,
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                cancel_if_open_below=order.cancel_if_open_below,
                cancel_if_open_above=order.cancel_if_open_above,
            )
            outcome = simulate_on_bar(spec, bar, self.costs)
            if outcome.action == "cancel":
                self._finish(order, OrderStatus.CANCELLED, bar, outcome.reason)
            elif outcome.action == "none":
                if order.time_in_force == TimeInForce.DAY:
                    self._finish(order, OrderStatus.EXPIRED, bar, "not filled in its session")
            elif order.side == OrderSide.BUY:
                stop_order = await self._fill_buy(order, bar, outcome, opens_at)
                if stop_order is not None:
                    queue.append(stop_order)  # protective stop is live for the rest of the bar
            else:
                await self._fill_sell(order, bar, outcome)

    async def _fill_buy(self, order, bar, outcome, opens_at) -> PaperOrderRecord | None:
        # Re-run the full risk gate at the actual fill price and current account state.
        recent = await get_latest_bars(
            self.session,
            raw_key(self.account, order.symbol),
            end=bar.timestamp - timedelta(microseconds=1),
            limit=self.limits.avg_volume_lookback,
        )
        others = [o for o in self.orders if o is not order and o.status == OrderStatus.WORKING]
        marks = await self.marks_at(bar.timestamp - _TICK, option_cutoff=bar.timestamp)
        state = risk_state(self.account, self.portfolio, marks, self.history, others)
        state = replace(state, equity=self.equity_before)
        if order.protective_stop_price is not None:
            intent = EntryIntent(
                order.symbol,
                order.quantity,
                outcome.base_price,
                order.protective_stop_price,
                bar.open,
                _mean_volume(recent),
            )
            decision = check_entry(intent, state, self.limits, self.costs)
            if not decision.approved:
                self._finish(
                    order,
                    OrderStatus.CANCELLED,
                    bar,
                    "risk check failed at fill: " + "; ".join(decision.failures),
                )
                return None
        commission = self.costs.commission(order.quantity)
        try:
            lines = self.portfolio.buy(
                order.symbol, order.quantity, outcome.fill_price, commission, opens_at
            )
        except AccountingError as exc:
            self._finish(order, OrderStatus.CANCELLED, bar, str(exc))
            return None
        fill = await self._record_fill(order, bar, outcome, commission, lines)
        if order.protective_stop_price is None:
            return None
        stop_order = PaperOrderRecord(
            account_id=self.account.id,
            symbol=order.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.STOP,
            time_in_force=TimeInForce.GTC,
            quantity=order.quantity,
            stop_price=order.protective_stop_price,
            parent_order_id=order.id,
            status=OrderStatus.WORKING,
            submitted_at=self.now,
            active_from=opens_at,
            risk_checks=[
                check(
                    "closes_existing_position",
                    True,
                    f"protective stop for fill {fill.id}",
                ).model_dump()
            ],
        )
        self.session.add(stop_order)
        await self.session.flush()
        self.orders.append(stop_order)
        return stop_order

    async def _fill_sell(self, order, bar, outcome) -> None:
        held = self.portfolio.positions.get(order.symbol)
        if held is None or held.quantity < order.quantity:
            self._finish(
                order,
                OrderStatus.CANCELLED,
                bar,
                f"position ({held.quantity if held else 0}) is smaller than the order",
            )
            return
        commission = self.costs.commission(order.quantity)
        lines = self.portfolio.sell(
            order.symbol, order.quantity, outcome.fill_price, commission, session_open(bar)
        )
        await self._record_fill(order, bar, outcome, commission, lines)
        if self.portfolio.positions[order.symbol].quantity == 0:
            for other in self.orders:
                if (
                    other.symbol == order.symbol
                    and other.side == OrderSide.SELL
                    and other.status == OrderStatus.WORKING
                ):
                    self._finish(other, OrderStatus.CANCELLED, bar, "position closed")

    async def _record_fill(self, order, bar, outcome, commission, lines) -> PaperFillRecord:
        fill = PaperFillRecord(
            account_id=self.account.id,
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            base_price=outcome.base_price,
            price=outcome.fill_price,
            commission=commission,
            bar_timestamp=bar.timestamp,
        )
        self.session.add(fill)
        await self.session.flush()
        await add_ledger(self.session, self.account.id, lines, fill_id=fill.id)
        order.filled_bar_at = bar.timestamp
        self._finish(order, OrderStatus.FILLED, bar, f"filled at {outcome.fill_price}", fill.id)
        return fill

    def _finish(self, order, status, bar, reason, fill_id=None) -> None:
        self.finish(order, status, bar.timestamp, reason, fill_id=fill_id)

    def finish(self, order, status, at, reason, *, symbol=None, fill_id=None) -> None:
        """Close `order` (if any) with `status` and record the event at `at`."""
        if order is not None:
            close_order(order, status, reason, self.now)
            if status != OrderStatus.FILLED:
                order.reserved_cost = None
        self.events.append(
            ProcessEvent(
                session=at,
                order_id=order.id if order is not None else None,
                symbol=order.symbol if order is not None else symbol,
                outcome=str(status),
                detail=reason,
                fill_id=fill_id,
            )
        )

    async def marks_at(self, ts: datetime, option_cutoff: datetime) -> dict[str, Decimal]:
        """Marks as they stood then: shares at the latest bar at or before `ts`, options
        at the bid of the latest quote before `option_cutoff`."""
        marks = {}
        for symbol in held_symbols(self.portfolio):
            contract = option_contract(symbol)
            if contract is not None:
                quote = await latest_quote(
                    self.session,
                    symbol,
                    self.options.settings.quote_feed,
                    at_or_before=option_cutoff - _TICK,
                )
                # No quote before the cutoff means the position was opened (in a later
                # run) from a later quote: carry it at cost rather than inventing a price.
                marks[symbol] = (
                    quote.bid if quote else self.portfolio.positions[symbol].average_cost
                )
                continue
            known = [b for b in self.bars.get(symbol, []) if b.timestamp <= ts]
            if known:
                marks[symbol] = known[-1].close
            else:
                latest = await get_latest_bars(
                    self.session, raw_key(self.account, symbol), end=ts, limit=1
                )
                if latest:
                    marks[symbol] = latest[-1].close
        return marks

    async def _valuation(self, ts: datetime, *, option_cutoff: datetime):
        return self.portfolio.valuation(await self.marks_at(ts, option_cutoff))


# --- local helpers ---------------------------------------------------------------------


async def _first_bar_after(
    session: AsyncSession, account: PaperAccountRecord, symbol: str, after: datetime
) -> MarketBarRecord | None:
    return await session.scalar(
        select(MarketBarRecord)
        .where(
            MarketBarRecord.symbol == symbol,
            MarketBarRecord.timeframe == TIMEFRAME,
            MarketBarRecord.feed == account.feed,
            MarketBarRecord.adjustment == Adjustment.RAW,
            MarketBarRecord.timestamp > after,
        )
        .order_by(MarketBarRecord.timestamp)
        .limit(1)
    )


def _mean_volume(bars: Sequence[MarketBarRecord]) -> Decimal | None:
    if not bars:
        return None
    return sum((b.volume for b in bars), Decimal(0)) / len(bars)


def _summary(account: PaperAccountRecord) -> AccountSummary:
    return AccountSummary(
        id=account.id,
        name=account.name,
        status=account.status,
        feed=account.feed,
        starting_cash=account.starting_cash,
        cash=account.cash,
        processed_through=account.processed_through,
        created_at=account.created_at,
    )


def _position_tuple(position) -> tuple:
    if position is None:
        return (0, Decimal(0), Decimal(0), 1)
    return (position.quantity, position.cost_basis, position.realized_pnl, position.multiplier)


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    if not denominator:
        return Decimal(0)
    return (numerator / denominator * _HUNDRED).quantize(Decimal("0.01"))
