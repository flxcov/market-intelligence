"""Helpers shared by the PAPER trading services (equities and options).

Sources of truth: the ledger (`paper_ledger`) for cash and positions; the
`cash` column and `paper_positions` table are caches checked by reconciliation.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from apps.marketpilot.execution.models import OrderSide, OrderStatus
from apps.marketpilot.execution.options import OptionSettings
from apps.marketpilot.execution.portfolio import LedgerKind, LedgerLine, Portfolio
from apps.marketpilot.risk.limits import AccountRiskState, RiskDecision
from apps.marketpilot.services.paper_models import OrderResult, OrderView
from packages.database.market_bars import SeriesKey, get_latest_bars
from packages.database.models import (
    PaperAccountRecord,
    PaperEquitySnapshotRecord,
    PaperLedgerRecord,
    PaperOrderRecord,
    PaperPositionRecord,
)
from packages.database.option_quotes import latest_quote
from packages.market_data.models import Adjustment, DataFeed
from packages.market_data.options import InvalidContractSymbolError, OptionContract
from packages.shared.errors import AppError

DAILY = timedelta(days=1)
TIMEFRAME = "1Day"
STALE_MARK_AFTER = timedelta(days=4)


class PaperNotFoundError(AppError):
    http_status = 404
    error_type = "not_found"


class PaperConflictError(AppError):
    http_status = 409
    error_type = "conflict"


class PaperRequestError(AppError):
    http_status = 400
    error_type = "invalid_paper_request"


@dataclass
class EquityHistory:
    """Session-close equities, for the daily-loss and drawdown limits."""

    starting_cash: Decimal
    equities: list[Decimal] = field(default_factory=list)

    @property
    def previous_session_equity(self) -> Decimal | None:
        if len(self.equities) >= 2:
            return self.equities[-2]
        return self.starting_cash if self.equities else None

    def peak(self, current: Decimal) -> Decimal:
        return max([self.starting_cash, current, *self.equities])


def raw_key(account: PaperAccountRecord, symbol: str) -> SeriesKey:
    # Fills and marks use RAW prices: what would actually have been paid.
    return SeriesKey(symbol, TIMEFRAME, DataFeed(account.feed), Adjustment.RAW)


async def get_account(
    session: AsyncSession, account_id: int, *, for_update: bool = False
) -> PaperAccountRecord:
    stmt = select(PaperAccountRecord).where(PaperAccountRecord.id == account_id)
    if for_update:
        stmt = stmt.with_for_update()  # serializes writers on the same account
    account = await session.scalar(stmt)
    if account is None:
        raise PaperNotFoundError(f"paper account {account_id} not found")
    return account


async def load_portfolio(session: AsyncSession, account_id: int) -> Portfolio:
    records = (
        await session.scalars(
            select(PaperLedgerRecord)
            .where(PaperLedgerRecord.account_id == account_id)
            .order_by(PaperLedgerRecord.id)
        )
    ).all()
    return Portfolio.replay(
        LedgerLine(
            kind=LedgerKind(r.kind),
            amount=r.amount,
            effective_at=r.effective_at,
            symbol=r.symbol,
            quantity=r.quantity,
            price=r.price,
            multiplier=r.multiplier,
        )
        for r in records
    )


async def add_ledger(
    session: AsyncSession, account_id: int, lines: list[LedgerLine], fill_id: int | None = None
) -> None:
    for line in lines:
        session.add(
            PaperLedgerRecord(
                account_id=account_id,
                kind=line.kind,
                amount=line.amount,
                symbol=line.symbol,
                quantity=line.quantity,
                price=line.price,
                multiplier=line.multiplier,
                fill_id=fill_id,
                effective_at=line.effective_at,
            )
        )
    await session.flush()


async def save_positions(session: AsyncSession, account_id: int, portfolio: Portfolio) -> None:
    for symbol, position in portfolio.positions.items():
        values = {
            "quantity": position.quantity,
            "multiplier": position.multiplier,
            "cost_basis": position.cost_basis,
            "realized_pnl": position.realized_pnl,
            "updated_at": func.now(),
        }
        await session.execute(
            insert(PaperPositionRecord)
            .values(account_id=account_id, symbol=symbol, **values)
            .on_conflict_do_update(index_elements=["account_id", "symbol"], set_=values)
        )


async def current_marks(
    session: AsyncSession, account: PaperAccountRecord, symbols: set[str], *, as_of: datetime
) -> tuple[dict[str, Decimal], dict[str, datetime], list[str]]:
    """Marks for open positions as of `as_of`.

    Shares: the latest COMPLETED raw daily close. Options: the BID of the latest
    stored quote (what the position could be sold for; 0 if nobody is bidding).
    """
    marks, times, notices = {}, {}, []
    for symbol in sorted(symbols):
        contract = option_contract(symbol)
        if contract is not None:
            mark, at, notice = await option_mark(session, account, contract, at_or_before=as_of)
            marks[symbol], times[symbol] = mark, at
            if as_of - at > STALE_MARK_AFTER:
                notices.append(f"{symbol} is valued at a stale quote ({at.date()}).")
            if notice:
                notices.append(notice)
            continue
        latest = await get_latest_bars(
            session, raw_key(account, symbol), end=as_of - DAILY, limit=1
        )
        if not latest:
            raise PaperConflictError(f"no completed raw bars to value open position {symbol}")
        marks[symbol] = latest[-1].close
        times[symbol] = latest[-1].timestamp
        if as_of - (latest[-1].timestamp + DAILY) > STALE_MARK_AFTER:
            notices.append(f"{symbol} is valued at a stale close ({latest[-1].timestamp.date()}).")
    return marks, times, notices


async def option_mark(
    session: AsyncSession,
    account: PaperAccountRecord,
    contract: OptionContract,
    *,
    at_or_before: datetime,
) -> tuple[Decimal, datetime, str | None]:
    feed = option_settings(account).quote_feed
    quote = await latest_quote(session, contract.symbol, feed, at_or_before=at_or_before)
    if quote is None:
        raise PaperConflictError(
            f"no stored {feed} quote to value open option position {contract.symbol}"
        )
    notice = None
    if at_or_before > contract.expires_at:
        notice = (
            f"{contract.symbol} expired on {contract.expiration} and is awaiting settlement; "
            "ingest the underlying's daily bar for that date and process the account."
        )
    return quote.bid, quote.quoted_at, notice


async def load_equity_history(session: AsyncSession, account: PaperAccountRecord) -> EquityHistory:
    equities = (
        await session.scalars(
            select(PaperEquitySnapshotRecord.equity)
            .where(PaperEquitySnapshotRecord.account_id == account.id)
            .order_by(PaperEquitySnapshotRecord.bar_at)
        )
    ).all()
    return EquityHistory(account.starting_cash, list(equities))


def risk_state(
    account: PaperAccountRecord,
    portfolio: Portfolio,
    marks: dict[str, Decimal],
    history: EquityHistory,
    working_orders: Sequence[PaperOrderRecord],
) -> AccountRiskState:
    valuation = portfolio.valuation(marks)
    pending = [
        o for o in working_orders if o.side == OrderSide.BUY and o.status == OrderStatus.WORKING
    ]
    return AccountRiskState(
        halted=account.status == "halted",
        equity=valuation.equity,
        cash=portfolio.cash,
        peak_equity=history.peak(valuation.equity),
        previous_equity=history.previous_session_equity,
        position_quantities={s: p.quantity for s, p in portfolio.positions.items() if p.quantity},
        gross_position_value=valuation.market_value,
        pending_entry_symbols=frozenset(o.symbol for o in pending),
        pending_entry_cost=sum((o.reserved_cost or Decimal(0) for o in pending), Decimal(0)),
    )


def held_symbols(portfolio: Portfolio) -> set[str]:
    return {s for s, p in portfolio.positions.items() if p.quantity}


def close_order(order: PaperOrderRecord, status: OrderStatus, reason: str, now: datetime) -> None:
    order.status = status
    order.status_reason = reason
    order.closed_at = now


def order_result(order: PaperOrderRecord, *, already_existed: bool = False) -> OrderResult:
    return OrderResult(
        order=OrderView.model_validate(order),
        accepted=order.status != OrderStatus.REJECTED,
        already_existed=already_existed,
    )


def finish_submission(order: PaperOrderRecord, decision: RiskDecision, now: datetime) -> None:
    order.risk_checks = [c.model_dump() for c in decision.checks]
    if decision.approved:
        order.status = OrderStatus.WORKING
    else:
        order.status = OrderStatus.REJECTED
        order.status_reason = "; ".join(decision.failures)
        order.closed_at = now
        order.reserved_cost = None


def option_contract(symbol: str) -> OptionContract | None:
    """The parsed contract if `symbol` is an OCC option symbol, else None (a share)."""
    try:
        return OptionContract.parse(symbol)
    except InvalidContractSymbolError:
        return None


def option_settings(account: PaperAccountRecord) -> OptionSettings:
    return OptionSettings(**(account.option_settings or {}))
