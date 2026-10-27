"""PAPER options: chain ingestion, order submission and the quote-driven simulation step.

Long calls and puts only (buy to open, sell to close). No writing, no spreads,
no exercise decisions, no margin. Options share the account's cash, ledger and
risk limits with shares.

Fills: see `execution.options` (ask for buys, bid for sells, displayed size
must cover the order, only quotes stamped after submission).

Order lifetime:
  - DAY orders can fill on quotes stamped before 16:00 Eastern on the day they
    were submitted, and expire then. Submitting a DAY order after that close is
    rejected (use GTC).
  - GTC orders work until the contract expires.

Expiry (simplified, disclosed): a position still held after the expiration-day
close is settled in CASH at intrinsic value, using the underlying's RAW daily
close on the expiration date from stored bars. In reality, in-the-money
contracts are normally exercised into shares (100 per contract), which then
carry overnight risk and possible fees; out-of-the-money contracts expire
worthless. Until that daily bar is stored, the position is reported as awaiting
settlement and is never settled with an invented price.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.execution.models import OrderSide, OrderStatus, TimeInForce
from apps.marketpilot.execution.options import (
    OptionSettings,
    check_option_entry,
    quote_fill,
)
from apps.marketpilot.execution.portfolio import AccountingError, Portfolio
from apps.marketpilot.risk.limits import RiskDecision, RiskLimits, check_exit
from apps.marketpilot.services.paper_common import (
    DAILY,
    EquityHistory,
    add_ledger,
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
)
from apps.marketpilot.services.paper_models import OptionOrderRequest, OrderResult
from apps.marketpilot.strategies.base import check
from packages.database.market_bars import get_latest_bars
from packages.database.models import OptionQuoteRecord, PaperFillRecord, PaperOrderRecord
from packages.database.option_quotes import (
    QuoteWriteCounts,
    insert_quotes,
    latest_chain,
    latest_quote,
    quotes_between,
)
from packages.market_data.options import (
    CONTRACT_MULTIPLIER,
    EASTERN,
    OPTION_FEED_NOTICES,
    OptionChainRequest,
    OptionContract,
    OptionFeed,
    SkippedContract,
    eastern_midnight,
    regular_close_on,
)
from packages.market_data.provider import MarketDataProvider
from packages.shared.timeutils import utc_now

logger = logging.getLogger(__name__)

OPTION = "option"
_TICK = timedelta(microseconds=1)


# --- chain ingestion and reads ----------------------------------------------------------


@dataclass(frozen=True)
class ChainIngestResult:
    underlying: str
    feed: OptionFeed
    provider: str
    retrieved_at: datetime
    received: int
    counts: QuoteWriteCounts
    skipped: list[SkippedContract]
    truncated: bool
    notices: list[str]


class OptionChainService:
    def __init__(
        self,
        provider: MarketDataProvider,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        default_feed: OptionFeed,
    ) -> None:
        self._provider = provider
        self._sessions = session_factory
        self._default_feed = default_feed

    async def ingest(self, request: OptionChainRequest) -> ChainIngestResult:
        request = request.model_copy(update={"feed": request.feed or self._default_feed})
        snapshot = await self._provider.get_option_chain(request)
        async with self._sessions.begin() as session:
            counts = await insert_quotes(
                session,
                snapshot.quotes,
                underlying=snapshot.underlying,
                feed=snapshot.feed,
                provider=snapshot.provider,
                retrieved_at=snapshot.retrieved_at,
            )
        notices = list(snapshot.notices)
        if counts.conflicting:
            notices.append(
                f"{counts.conflicting} quote(s) differed from an already stored quote with the "
                "same contract and timestamp; the stored quote was kept."
            )
        logger.info(
            "option chain ingested",
            extra={
                "underlying": snapshot.underlying,
                "feed": snapshot.feed.value,
                "inserted": counts.inserted,
                "unchanged": counts.unchanged,
                "conflicting": counts.conflicting,
            },
        )
        return ChainIngestResult(
            underlying=snapshot.underlying,
            feed=snapshot.feed,
            provider=snapshot.provider,
            retrieved_at=snapshot.retrieved_at,
            received=len(snapshot.quotes),
            counts=counts,
            skipped=snapshot.skipped,
            truncated=snapshot.truncated,
            notices=notices,
        )


async def read_chain(
    session_factory: async_sessionmaker[AsyncSession],
    underlying: str,
    feed: OptionFeed,
    *,
    as_of: datetime,
    expiration=None,
    limit: int,
) -> list[OptionQuoteRecord]:
    async with session_factory() as session:
        return await latest_chain(
            session, underlying, feed, at_or_before=as_of, expiration=expiration, limit=limit
        )


def feed_notice(feed: OptionFeed) -> str:
    return OPTION_FEED_NOTICES[feed]


# --- order submission ------------------------------------------------------------------


class OptionTradingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._now = now

    async def submit_order(
        self, account_id: int, request: OptionOrderRequest, *, deployment_id: int | None = None
    ) -> OrderResult:
        now = self._now()
        contract = OptionContract.parse(request.symbol)
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

            settings = option_settings(account)
            limits = RiskLimits(**account.risk_limits)
            portfolio = await load_portfolio(session, account_id)
            marks, _, _ = await current_marks(session, account, held_symbols(portfolio), as_of=now)
            working = (
                await session.scalars(
                    select(PaperOrderRecord).where(
                        PaperOrderRecord.account_id == account_id,
                        PaperOrderRecord.status == OrderStatus.WORKING,
                    )
                )
            ).all()
            state = risk_state(
                account, portfolio, marks, await load_equity_history(session, account), working
            )
            quote = await latest_quote(
                session, contract.symbol, settings.quote_feed, at_or_before=now
            )

            order = PaperOrderRecord(
                account_id=account_id,
                symbol=contract.symbol,
                asset_class=OPTION,
                multiplier=CONTRACT_MULTIPLIER,
                side=request.side,
                order_type=request.order_type,
                time_in_force=request.time_in_force,
                limit_price=request.limit_price,
                client_order_id=request.client_order_id,
                deployment_id=deployment_id,
                submitted_at=now,
                active_from=now,
            )
            timing = _timing_check(request.time_in_force, now)
            if request.side is OrderSide.BUY:
                order.quantity = request.quantity or 0
                decision = self._gate_buy(
                    order, contract, quote, request, now, state, limits, settings
                )
            else:
                order.quantity = request.quantity or state.position_quantities.get(
                    contract.symbol, 0
                )
                decision = check_exit(contract.symbol, order.quantity, state)
            if timing is not None:
                decision = RiskDecision(False, [*decision.checks, timing])
            finish_submission(order, decision, now)
            session.add(order)
            await session.flush()
        logger.info(
            "paper option order submitted",
            extra={"account_id": account_id, "order_id": order.id, "status": order.status},
        )
        return order_result(order)

    @staticmethod
    def _gate_buy(order, contract, quote, request, now, state, limits, settings) -> RiskDecision:
        if quote is None:
            return RiskDecision(
                False,
                [
                    check(
                        "market_data_available",
                        False,
                        f"no stored {settings.quote_feed} quote for {contract.symbol}; "
                        "ingest the option chain first",
                    )
                ],
            )
        # The most it may pay per share: its limit, or the ask it can see now. A market
        # order filling later at a higher ask is re-checked at that moment.
        worst = request.limit_price if request.limit_price is not None else quote.ask
        decision, risk = check_option_entry(
            contract,
            order.quantity,
            worst,
            quote,
            at=now,
            state=state,
            limits=limits,
            settings=settings,
            check_quote_age=True,
        )
        order.reserved_cost = risk.premium_at_risk
        order.sizing = {
            **risk.as_dict(),
            "quote": {
                "quoted_at": quote.quoted_at.isoformat(),
                "bid": str(quote.bid),
                "ask": str(quote.ask),
                "bid_size": str(quote.bid_size),
                "ask_size": str(quote.ask_size),
                "feed": settings.quote_feed.value,
            },
        }
        return decision


def _timing_check(time_in_force: TimeInForce, now: datetime):
    if time_in_force is TimeInForce.DAY and now >= regular_close_on(now.astimezone(EASTERN).date()):
        return check(
            "day_order_session_open",
            False,
            "a DAY option order submitted after 16:00 Eastern could never fill; use gtc",
        )
    return None


def order_deadline(order: PaperOrderRecord, contract: OptionContract) -> datetime:
    """The moment a working option order stops being fillable."""
    if order.time_in_force == TimeInForce.DAY:
        return min(
            regular_close_on(order.submitted_at.astimezone(EASTERN).date()), contract.expires_at
        )
    return contract.expires_at


# --- the simulation step ----------------------------------------------------------------


class SimulationContext(Protocol):
    """The parts of the PAPER simulation run the options step works with."""

    session: AsyncSession
    account: object
    portfolio: Portfolio
    history: EquityHistory
    limits: RiskLimits
    orders: list[PaperOrderRecord]
    notices: list[str]

    def finish(
        self,
        order: PaperOrderRecord | None,
        status: str,
        at: datetime,
        reason: str,
        *,
        symbol: str | None = None,
        fill_id: int | None = None,
    ) -> None: ...

    async def marks_at(self, ts: datetime, option_cutoff: datetime) -> dict[str, Decimal]: ...


class OptionSimulation:
    """Fills working option orders from stored quotes and settles expired positions.

    `step(through)` considers only quotes stamped before `through` and settles only
    with daily bars complete by `through`. Every action is idempotent: running a
    step again (or with a later `through`) never repeats a fill or a settlement.
    """

    def __init__(self, ctx: SimulationContext) -> None:
        self.ctx = ctx
        self.settings: OptionSettings = option_settings(ctx.account)
        self._reported: set[str] = set()

    def working(self) -> list[PaperOrderRecord]:
        return [
            o
            for o in self.ctx.orders
            if o.asset_class == OPTION and o.status == OrderStatus.WORKING
        ]

    def underlyings(self) -> set[str]:
        """Underlying tickers whose daily bars the run needs (settlement, marks)."""
        symbols = {o.symbol for o in self.working()} | {
            s for s in held_symbols(self.ctx.portfolio) if option_contract(s)
        }
        return {OptionContract.parse(s).root for s in symbols}

    async def step(self, through: datetime) -> None:
        await self._fill(through)
        await self._settle(through)
        for order in self.working():
            deadline = order_deadline(order, OptionContract.parse(order.symbol))
            if deadline < through:
                self.ctx.finish(
                    order, OrderStatus.EXPIRED, deadline, "not filled before its deadline"
                )

    async def _fill(self, through: datetime) -> None:
        feed = self.settings.quote_feed
        queue: list[tuple[datetime, int, PaperOrderRecord, OptionQuoteRecord]] = []
        for order in self.working():
            deadline = order_deadline(order, OptionContract.parse(order.symbol))
            quotes = await quotes_between(
                self.ctx.session,
                order.symbol,
                feed,
                after=order.active_from,
                before=min(through, deadline),
            )
            queue += [(q.quoted_at, order.id, order, q) for q in quotes]
        # Strict time order across contracts, so cash is spent in the order it would be.
        queue.sort(key=lambda item: (item[0], item[1]))
        for _, _, order, quote in queue:
            if order.status != OrderStatus.WORKING:
                continue
            fill = quote_fill(OrderSide(order.side), order.quantity, order.limit_price, quote)
            if not fill.filled:
                continue
            assert fill.price is not None
            if order.side == OrderSide.BUY:
                await self._buy(order, quote, fill.price)
            else:
                await self._sell(order, quote, fill.price)

    async def _buy(self, order: PaperOrderRecord, quote: OptionQuoteRecord, price: Decimal) -> None:
        ctx, at = self.ctx, quote.quoted_at
        contract = OptionContract.parse(order.symbol)
        others = [o for o in ctx.orders if o is not order and o.status == OrderStatus.WORKING]
        marks = await ctx.marks_at(at - DAILY, at + _TICK)
        state = risk_state(ctx.account, ctx.portfolio, marks, ctx.history, others)
        decision, _ = check_option_entry(
            contract,
            order.quantity,
            price,
            quote,
            at=at,
            state=state,
            limits=ctx.limits,
            settings=self.settings,
            check_quote_age=False,
        )
        if not decision.approved:
            ctx.finish(
                order,
                OrderStatus.CANCELLED,
                at,
                "risk check failed at fill: " + "; ".join(decision.failures),
            )
            return
        commission = self.settings.commission(order.quantity)
        try:
            lines = ctx.portfolio.buy(
                order.symbol, order.quantity, price, commission, at, CONTRACT_MULTIPLIER
            )
        except AccountingError as exc:
            ctx.finish(order, OrderStatus.CANCELLED, at, str(exc))
            return
        await self._record_fill(order, quote, price, commission, lines)

    async def _sell(
        self, order: PaperOrderRecord, quote: OptionQuoteRecord, price: Decimal
    ) -> None:
        ctx, at = self.ctx, quote.quoted_at
        held = ctx.portfolio.positions.get(order.symbol)
        if held is None or held.quantity < order.quantity:
            ctx.finish(
                order,
                OrderStatus.CANCELLED,
                at,
                f"position ({held.quantity if held else 0}) is smaller than the order",
            )
            return
        commission = self.settings.commission(order.quantity)
        lines = ctx.portfolio.sell(
            order.symbol, order.quantity, price, commission, at, CONTRACT_MULTIPLIER
        )
        await self._record_fill(order, quote, price, commission, lines)
        if ctx.portfolio.positions[order.symbol].quantity == 0:
            self._cancel_sells(order.symbol, at, "position closed")

    async def _record_fill(self, order, quote, price, commission, lines) -> None:
        ctx = self.ctx
        fill = PaperFillRecord(
            account_id=ctx.account.id,
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            multiplier=CONTRACT_MULTIPLIER,
            base_price=price,
            price=price,
            commission=commission,
            bar_timestamp=quote.quoted_at,
        )
        ctx.session.add(fill)
        await ctx.session.flush()
        await add_ledger(ctx.session, ctx.account.id, lines, fill_id=fill.id)
        order.filled_bar_at = quote.quoted_at
        side = "ask" if order.side == OrderSide.BUY else "bid"
        ctx.finish(
            order,
            OrderStatus.FILLED,
            quote.quoted_at,
            f"filled {order.quantity} contract(s) at the {side} {price} "
            f"(quote {quote.quoted_at.isoformat()}, {self.settings.quote_feed} feed)",
            fill_id=fill.id,
        )

    async def _settle(self, through: datetime) -> None:
        ctx = self.ctx
        for symbol in sorted(held_symbols(ctx.portfolio)):
            contract = option_contract(symbol)
            if contract is None:
                continue
            bar_at = eastern_midnight(contract.expiration)
            if bar_at + DAILY > through:
                continue  # the expiration-day bar is not complete yet at this point
            bars = await get_latest_bars(
                ctx.session, raw_key(ctx.account, contract.root), end=bar_at, limit=1
            )
            if not bars or bars[-1].timestamp != bar_at:
                if symbol not in self._reported:
                    self._reported.add(symbol)
                    ctx.notices.append(
                        f"{symbol} expired on {contract.expiration} but no raw {ctx.account.feed} "
                        f"daily bar for {contract.root} on that date is stored, so it cannot be "
                        "settled yet. Ingest it and process again."
                    )
                continue
            close = bars[-1].close
            intrinsic = contract.intrinsic_value(close)
            quantity = ctx.portfolio.positions[symbol].quantity
            lines = ctx.portfolio.expire(
                symbol, intrinsic, contract.expires_at, CONTRACT_MULTIPLIER
            )
            await add_ledger(ctx.session, ctx.account.id, lines)
            state = "in the money" if intrinsic > 0 else "out of the money (worthless)"
            ctx.finish(
                None,
                "settled",
                contract.expires_at,
                f"expired {state}: {contract.root} closed at {close} vs strike "
                f"{contract.strike}; {quantity} contract(s) cash-settled at {intrinsic} per "
                f"share ({lines[0].amount} total). Simplification: real ITM contracts are "
                "usually exercised into shares.",
                symbol=symbol,
            )
            self._cancel_sells(symbol, contract.expires_at, "contract expired")

    def _cancel_sells(self, symbol: str, at: datetime, reason: str) -> None:
        for other in self.ctx.orders:
            if (
                other.symbol == symbol
                and other.side == OrderSide.SELL
                and other.status == OrderStatus.WORKING
            ):
                self.ctx.finish(other, OrderStatus.CANCELLED, at, reason)
