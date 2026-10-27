"""PAPER trading service against a real database, with a controllable clock.

Synthetic raw daily bars are stamped 05:00Z (midnight U.S. Eastern in winter), so
session n opens at day(n) + 9h30m. Defaults: 7 bps adverse costs, no commission,
0.5% risk per trade, 10% max position, 1% of average volume.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.execution.models import OrderSide, OrderStatus, OrderType, TimeInForce
from apps.marketpilot.services.paper_models import (
    CreateAccountRequest,
    ManualOrderRequest,
    SignalOrderRequest,
)
from apps.marketpilot.services.paper_trading import (
    PaperConflictError,
    PaperRequestError,
    PaperTradingService,
)
from apps.marketpilot.services.signals import SignalGenerationRequest, SignalService
from packages.database.market_bars import SeriesKey, upsert_bars
from packages.database.models import PaperEquitySnapshotRecord, PaperOrderRecord
from packages.market_data.models import Adjustment, Bar, DataFeed
from tests.strategy_data import as_of_after, breakout_series

pytestmark = pytest.mark.anyio

D = Decimal
Sessions = async_sessionmaker[AsyncSession]
SYMBOL = "PAPR"
START = datetime(2024, 1, 1, 5, tzinfo=UTC)


def day(n: int) -> datetime:
    return START + timedelta(days=n)


def before_open(n: int) -> datetime:
    """A moment after session n-1 completed and before session n opens."""
    return day(n) + timedelta(hours=1)


def after_close(n: int) -> datetime:
    """A moment after session n's daily bar has completed."""
    return day(n + 1) + timedelta(hours=1)


def bar(n: int, o, h, low, c, v=1_000_000) -> Bar:
    return Bar(timestamp=day(n), open=D(o), high=D(h), low=D(low), close=D(c), volume=D(v))


HISTORY = [bar(n, "50", "50.5", "49.5", "50") for n in range(1, 26)]  # sessions 1..25


async def store(sessions: Sessions, bars: list[Bar], symbol=SYMBOL, adjustment=Adjustment.RAW):
    async with sessions.begin() as session:
        await upsert_bars(
            session,
            SeriesKey(symbol, "1Day", DataFeed.IEX, adjustment),
            bars,
            provider="synthetic",
            retrieved_at=day(100),
        )


def service(sessions: Sessions, now: datetime) -> PaperTradingService:
    return PaperTradingService(sessions, default_feed=DataFeed.IEX, now=lambda: now)


async def new_account(sessions: Sessions, now: datetime | None = None, **kwargs) -> int:
    now = now or before_open(26)
    request = CreateAccountRequest(
        name=kwargs.pop("name", "test"), starting_cash=D(100_000), **kwargs
    )
    return (await service(sessions, now).create_account(request)).id


def buy(**kwargs) -> ManualOrderRequest:
    return ManualOrderRequest(
        **{"symbol": SYMBOL, "side": "buy", "protective_stop_price": D(48), **kwargs}
    )


async def orders(sessions: Sessions, account_id: int) -> list[PaperOrderRecord]:
    async with sessions() as session:
        stmt = select(PaperOrderRecord).where(PaperOrderRecord.account_id == account_id)
        return list((await session.scalars(stmt.order_by(PaperOrderRecord.id))).all())


async def assert_balanced(sessions: Sessions, account_id: int, now: datetime) -> None:
    result = await service(sessions, now).reconcile(account_id)
    assert result.balanced, [c for c in result.checks if not c.passed]


# --- accounts ------------------------------------------------------------------------


async def test_new_account_is_funded_and_balanced(sessions: Sessions) -> None:
    account_id = await new_account(sessions)
    view = await service(sessions, before_open(26)).account_view(account_id)
    assert (view.mode, view.cash, view.equity, view.drawdown_pct) == (
        "PAPER",
        D(100_000),
        D(100_000),
        D(0),
    )
    ledger = await service(sessions, before_open(26)).list_ledger(account_id, limit=10)
    assert [(e.kind, e.amount) for e in ledger] == [("deposit", D(100_000))]
    await assert_balanced(sessions, account_id, before_open(26))


async def test_account_names_are_unique(sessions: Sessions) -> None:
    await new_account(sessions)
    with pytest.raises(PaperConflictError):
        await new_account(sessions)


# --- the full lifecycle: sized entry, fill, protective stop, stop-out ---------------------


async def test_entry_fill_then_stop_out(sessions: Sessions) -> None:
    await store(sessions, HISTORY)
    account_id = await new_account(sessions)

    submitted = await service(sessions, before_open(26)).submit_order(account_id, buy())
    order = submitted.order
    assert submitted.accepted and order.status == "working"
    # Sized at the last close (50): position cap 10,000 / 50.04 = 199 binds before risk (240).
    assert order.quantity == 199
    assert order.sizing["limiting_factor"] == "position"
    assert order.reserved_cost == D("9957.96")  # 199 x 50.04

    await store(sessions, [bar(26, "50", "51", "49.8", "50.8")])
    result = await service(sessions, after_close(26)).process(account_id)
    assert [(e.outcome, e.symbol) for e in result.events] == [("filled", SYMBOL)]
    assert result.sessions_processed == [day(26)]

    view = await service(sessions, after_close(26)).account_view(account_id)
    assert view.cash == D("90042.04")  # 100,000 - 199 x 50.04
    assert view.equity == D("100151.24")  # cash + 199 x 50.80
    (position,) = view.positions
    assert (position.quantity, position.average_cost, position.unrealized_pnl) == (
        199,
        D("50.0400"),
        D("151.24"),
    )
    stop = [o for o in await orders(sessions, account_id) if o.parent_order_id == order.id]
    assert len(stop) == 1
    assert (stop[0].side, stop[0].order_type, stop[0].stop_price, stop[0].status) == (
        "sell",
        "stop",
        D(48),
        "working",
    )

    # Next session trades down through the stop: filled at 48 less 7 bps -> 47.96.
    await store(sessions, [bar(27, "49", "49.2", "47.5", "47.8")])
    result = await service(sessions, after_close(27)).process(account_id)
    assert [e.outcome for e in result.events] == ["filled"]

    view = await service(sessions, after_close(27)).account_view(account_id)
    assert view.cash == view.equity == D("99586.08")  # 90,042.04 + 199 x 47.96
    assert view.realized_pnl == D("-413.92")  # (47.96 - 50.04) x 199
    assert view.unrealized_pnl == D(0)
    assert view.equity - view.starting_cash == view.realized_pnl - view.fees
    await assert_balanced(sessions, account_id, after_close(27))


async def test_gap_through_stop_fills_at_the_worse_open(sessions: Sessions) -> None:
    await store(sessions, [*HISTORY, bar(26, "50", "51", "49.8", "50.8")])
    account_id = await new_account(sessions)
    await service(sessions, before_open(26)).submit_order(account_id, buy())
    await service(sessions, after_close(26)).process(account_id)

    await store(sessions, [bar(27, "45", "46", "44", "45.5")])  # opens far below the 48 stop
    await service(sessions, after_close(27)).process(account_id)

    (sell_fill, _buy_fill) = await service(sessions, after_close(27)).list_fills(
        account_id, limit=5
    )
    assert (sell_fill.side, sell_fill.base_price, sell_fill.price) == ("sell", D(45), D("44.96"))


async def test_stop_can_trigger_in_the_entry_session(sessions: Sessions) -> None:
    await store(sessions, [*HISTORY, bar(26, "50", "50.2", "47", "47.5")])
    account_id = await new_account(sessions)
    await service(sessions, before_open(26)).submit_order(account_id, buy())

    result = await service(sessions, after_close(26)).process(account_id)

    assert [e.outcome for e in result.events] == ["filled", "filled"]
    view = await service(sessions, after_close(26)).account_view(account_id)
    assert view.positions[0].quantity == 0
    assert view.realized_pnl == D("-413.92")


async def test_gap_up_that_breaks_the_risk_budget_cancels_at_fill(sessions: Sessions) -> None:
    await store(sessions, [*HISTORY, bar(26, "50.5", "51", "50", "50.8")])
    account_id = await new_account(sessions)
    await service(sessions, before_open(26)).submit_order(account_id, buy())

    result = await service(sessions, after_close(26)).process(account_id)

    # Fill would be 50.54: risk (50.54 - 47.96) x 199 = 513.42 > 500 budget.
    (event,) = result.events
    assert event.outcome == "cancelled"
    assert "risk check failed at fill" in event.detail and "per_trade_risk" in event.detail
    assert (await service(sessions, after_close(26)).account_view(account_id)).cash == D(100_000)


# --- order timing --------------------------------------------------------------------------


async def test_order_submitted_after_the_open_waits_for_the_next_session(
    sessions: Sessions,
) -> None:
    await store(sessions, HISTORY)
    account_id = await new_account(sessions, now=before_open(26))
    late = day(26) + timedelta(hours=12)  # session 26 opened at 14:30Z
    await service(sessions, late).submit_order(account_id, buy(time_in_force="gtc"))

    await store(
        sessions, [bar(26, "50", "50.2", "49.9", "50"), bar(27, "50", "50.3", "49.9", "50.1")]
    )
    result = await service(sessions, after_close(27)).process(account_id)

    (event,) = result.events
    assert (event.outcome, event.session) == ("filled", day(27))


async def test_unfilled_day_limit_order_expires(sessions: Sessions) -> None:
    await store(sessions, [*HISTORY, bar(26, "50", "50.5", "49.5", "50")])
    account_id = await new_account(sessions)
    limit = buy(order_type="limit", limit_price=D("49.5"), protective_stop_price=D(47), quantity=10)
    await service(sessions, before_open(26)).submit_order(account_id, limit)

    result = await service(sessions, after_close(26)).process(account_id)

    # The low equals the limit: touching is not a fill.
    assert [(e.outcome, e.detail) for e in result.events] == [
        ("expired", "not filled in its session")
    ]


async def test_processing_is_idempotent(sessions: Sessions) -> None:
    await store(sessions, [*HISTORY, bar(26, "50", "51", "49.8", "50.8")])
    account_id = await new_account(sessions)
    await service(sessions, before_open(26)).submit_order(account_id, buy())

    first = await service(sessions, after_close(26)).process(account_id)
    second = await service(sessions, after_close(26) + timedelta(hours=1)).process(account_id)

    assert len(first.events) == 1
    assert (second.events, second.sessions_processed) == ([], [])
    assert len(await service(sessions, after_close(26)).list_fills(account_id, limit=10)) == 1


async def test_process_rejects_future_as_of(sessions: Sessions) -> None:
    account_id = await new_account(sessions)
    with pytest.raises(PaperRequestError):
        await service(sessions, before_open(26)).process(account_id, before_open(27))


# --- risk gate at submission ------------------------------------------------------------


async def test_rejected_orders_are_recorded_with_reasons(sessions: Sessions) -> None:
    await store(sessions, HISTORY)
    account_id = await new_account(sessions)

    result = await service(sessions, before_open(26)).submit_order(account_id, buy(quantity=5000))

    assert result.accepted is False
    assert result.order.status == "rejected"
    assert "per_trade_risk" in result.order.status_reason
    assert "max_position_size" in result.order.status_reason
    assert result.order.reserved_cost is None


async def test_buy_without_market_data_is_rejected(sessions: Sessions) -> None:
    account_id = await new_account(sessions)
    result = await service(sessions, before_open(26)).submit_order(account_id, buy())
    assert (
        result.order.status_reason == "market_data_available: no completed raw bars for this symbol"
    )


async def test_second_entry_in_same_symbol_is_rejected(sessions: Sessions) -> None:
    await store(sessions, HISTORY)
    account_id = await new_account(sessions)
    svc = service(sessions, before_open(26))
    await svc.submit_order(account_id, buy())
    second = await svc.submit_order(account_id, buy())
    assert "one_position_per_symbol" in second.order.status_reason


async def test_client_order_id_makes_submission_idempotent(sessions: Sessions) -> None:
    await store(sessions, HISTORY)
    account_id = await new_account(sessions)
    svc = service(sessions, before_open(26))
    first = await svc.submit_order(account_id, buy(client_order_id="abc-1"))
    again = await svc.submit_order(account_id, buy(client_order_id="abc-1"))
    assert (again.order.id, again.already_existed) == (first.order.id, True)
    assert len(await orders(sessions, account_id)) == 1


async def test_cannot_sell_what_is_not_held(sessions: Sessions) -> None:
    await store(sessions, HISTORY)
    account_id = await new_account(sessions)
    result = await service(sessions, before_open(26)).submit_order(
        account_id, ManualOrderRequest(symbol=SYMBOL, side="sell", quantity=1)
    )
    assert "closes_existing_position" in result.order.status_reason


async def test_daily_loss_limit_blocks_new_entries(sessions: Sessions) -> None:
    await store(sessions, [*HISTORY, bar(26, "50", "51", "49.8", "50.8")])
    account_id = await new_account(
        sessions, risk_limits={"max_daily_loss_pct": "0.5"}, name="tight"
    )
    await service(sessions, before_open(26)).submit_order(account_id, buy())
    await service(sessions, after_close(26)).process(account_id)
    # Session 27 closes at 48.1 without hitting the 48 stop: (50.8 - 48.1) x 199 = 537.30 lost,
    # 0.54% of 100,151.24 equity, beyond the 0.5% daily limit.
    await store(sessions, [bar(27, "50", "50.2", "48.05", "48.1")])
    await store(sessions, HISTORY, symbol="OTHR")
    await service(sessions, after_close(27)).process(account_id)

    other = buy(symbol="OTHR", protective_stop_price=D(48))
    result = await service(sessions, before_open(28)).submit_order(account_id, other)

    assert result.accepted is False
    assert "daily_loss_limit" in result.order.status_reason


# --- halt / resume -------------------------------------------------------------------------


async def test_halt_cancels_buys_blocks_entries_and_allows_exits(sessions: Sessions) -> None:
    await store(sessions, [*HISTORY, bar(26, "50", "51", "49.8", "50.8")])
    await store(sessions, HISTORY, symbol="OTHR")
    account_id = await new_account(sessions)
    svc = service(sessions, before_open(26))
    await svc.submit_order(account_id, buy())
    pending = await svc.submit_order(account_id, buy(symbol="OTHR"))
    await service(sessions, after_close(26)).process(account_id)  # PAPR fills; OTHR has no bar

    halted = await service(sessions, before_open(27)).set_halted(account_id, True)
    assert halted.status == "halted"
    records = {o.id: o for o in await orders(sessions, account_id)}
    assert records[pending.order.id].status == "cancelled"
    assert records[pending.order.id].status_reason == "account halted"
    protective = [o for o in records.values() if o.order_type == "stop"]
    assert protective[0].status == "working"  # risk-reducing orders stay active

    svc = service(sessions, before_open(27))
    blocked = await svc.submit_order(account_id, buy(symbol="OTHR"))
    assert "account_active" in blocked.order.status_reason
    exit_ = await svc.submit_order(account_id, ManualOrderRequest(symbol=SYMBOL, side="sell"))
    assert exit_.accepted and exit_.order.quantity == 199

    assert (await svc.set_halted(account_id, False)).status == "active"


async def test_manual_exit_cancels_the_protective_stop_once_flat(sessions: Sessions) -> None:
    await store(sessions, [*HISTORY, bar(26, "50", "51", "49.8", "50.8")])
    account_id = await new_account(sessions)
    await service(sessions, before_open(26)).submit_order(account_id, buy())
    await service(sessions, after_close(26)).process(account_id)
    await service(sessions, before_open(27)).submit_order(
        account_id, ManualOrderRequest(symbol=SYMBOL, side="sell")
    )

    await store(sessions, [bar(27, "51", "51.5", "50.5", "51")])
    result = await service(sessions, after_close(27)).process(account_id)

    assert sorted(e.outcome for e in result.events) == ["cancelled", "filled"]
    stop = next(o for o in await orders(sessions, account_id) if o.order_type == "stop")
    assert (stop.status, stop.status_reason) == ("cancelled", "position closed")
    await assert_balanced(sessions, account_id, after_close(27))


# --- signal orders ---------------------------------------------------------------------------


async def signal_setup(sessions: Sessions) -> tuple[int, datetime, int]:
    bars = breakout_series()  # SYNTHETIC; split-adjusted == raw here (no splits)
    await store(sessions, bars, symbol="UP", adjustment=Adjustment.SPLIT)
    await store(sessions, bars, symbol="UP", adjustment=Adjustment.RAW)
    as_of = as_of_after(bars)
    generated = await SignalService(sessions, default_feed=DataFeed.IEX).generate(
        SignalGenerationRequest(symbols=["UP"], as_of=as_of)
    )
    signal_id = generated.results[0].signal_id
    assert signal_id is not None
    account_id = await new_account(sessions, now=as_of)
    return signal_id, bars[-1].timestamp, account_id


async def test_signal_order_is_sized_risk_checked_and_filled(sessions: Sessions) -> None:
    signal_id, signal_bar, account_id = await signal_setup(sessions)
    submit_at = signal_bar + timedelta(days=1, hours=2)  # before the next session opens

    result = await service(sessions, submit_at).submit_signal_order(
        account_id, SignalOrderRequest(signal_id=signal_id)
    )
    order = result.order
    assert result.accepted
    assert (order.order_type, order.time_in_force, order.signal_id) == ("market", "day", signal_id)
    assert (order.cancel_if_open_below, order.cancel_if_open_above) == (
        D("139.5857"),
        D("142.9071"),
    )
    # Synthetic volume ~1,000-1,500/day: 1% liquidity cap binds at 10 shares.
    assert (order.quantity, order.sizing["limiting_factor"]) == (10, "liquidity")

    again = await service(sessions, submit_at).submit_signal_order(
        account_id, SignalOrderRequest(signal_id=signal_id)
    )
    assert (again.order.id, again.already_existed) == (order.id, True)

    entry = Bar(
        timestamp=signal_bar + timedelta(days=1),
        open=D(142),
        high=D("142.5"),
        low=D("141.5"),
        close=D(142),
        volume=D(1200),
    )
    await store(sessions, [entry], symbol="UP")
    result = await service(sessions, entry.timestamp + timedelta(days=1, hours=1)).process(
        account_id
    )
    assert [e.outcome for e in result.events] == ["filled"]
    (fill,) = await service(sessions, submit_at).list_fills(account_id, limit=5)
    assert (fill.quantity, fill.price) == (10, D("142.10"))  # 142 x 1.0007 = 142.0994 rounded up


async def test_signal_order_cancelled_when_open_gaps_above_bound(sessions: Sessions) -> None:
    signal_id, signal_bar, account_id = await signal_setup(sessions)
    await service(sessions, signal_bar + timedelta(days=1, hours=2)).submit_signal_order(
        account_id, SignalOrderRequest(signal_id=signal_id)
    )
    gap = Bar(
        timestamp=signal_bar + timedelta(days=1),
        open=D(144),
        high=D(145),
        low=D(143),
        close=D(144),
        volume=D(1200),
    )
    await store(sessions, [gap], symbol="UP")
    result = await service(sessions, gap.timestamp + timedelta(days=1, hours=1)).process(account_id)
    (event,) = result.events
    assert (event.outcome, event.detail) == ("cancelled", "open 144 above 142.9071")


async def test_signal_order_after_entry_session_opened_is_rejected(sessions: Sessions) -> None:
    signal_id, signal_bar, account_id = await signal_setup(sessions)
    entry = Bar(
        timestamp=signal_bar + timedelta(days=1),
        open=D(142),
        high=D("142.5"),
        low=D("141.5"),
        close=D(142),
        volume=D(1200),
    )
    await store(sessions, [entry], symbol="UP")
    too_late = entry.timestamp + timedelta(hours=12)

    result = await service(sessions, too_late).submit_signal_order(
        account_id, SignalOrderRequest(signal_id=signal_id)
    )
    assert result.accepted is False
    assert "signal_still_valid" in result.order.status_reason


async def test_signal_from_other_feed_is_refused(sessions: Sessions) -> None:
    signal_id, signal_bar, _ = await signal_setup(sessions)
    sip_account = await new_account(
        sessions, now=signal_bar + timedelta(days=1), feed="sip", name="sip"
    )
    with pytest.raises(PaperRequestError, match="feed"):
        await service(sessions, signal_bar + timedelta(days=1, hours=2)).submit_signal_order(
            sip_account, SignalOrderRequest(signal_id=signal_id)
        )


# --- reconciliation ------------------------------------------------------------------------


async def test_reconcile_detects_tampering(sessions: Sessions) -> None:
    await store(sessions, [*HISTORY, bar(26, "50", "51", "49.8", "50.8")])
    account_id = await new_account(sessions)
    await service(sessions, before_open(26)).submit_order(account_id, buy())
    await service(sessions, after_close(26)).process(account_id)
    await assert_balanced(sessions, account_id, after_close(26))

    async with sessions.begin() as session:
        await session.execute(
            text("UPDATE paper_accounts SET cash = cash + 1 WHERE id = :id"), {"id": account_id}
        )
        await session.execute(
            text("UPDATE paper_positions SET quantity = 1 WHERE account_id = :id"),
            {"id": account_id},
        )
    result = await service(sessions, after_close(26)).reconcile(account_id)
    failed = {c.rule for c in result.checks if not c.passed}
    assert result.balanced is False
    assert failed == {"cash_matches_ledger", "positions_match_ledger"}


async def test_equity_snapshots_are_written_per_session(sessions: Sessions) -> None:
    await store(
        sessions,
        [*HISTORY, bar(26, "50", "51", "49.8", "50.8"), bar(27, "51", "52", "50.5", "51.5")],
    )
    account_id = await new_account(sessions)
    await service(sessions, before_open(26)).submit_order(account_id, buy())
    await service(sessions, after_close(27)).process(account_id)

    async with sessions() as session:
        snapshots = (
            await session.scalars(
                select(PaperEquitySnapshotRecord)
                .where(PaperEquitySnapshotRecord.account_id == account_id)
                .order_by(PaperEquitySnapshotRecord.bar_at)
            )
        ).all()
    assert [(s.bar_at, s.equity) for s in snapshots] == [
        (day(26), D("100151.24")),  # 90,042.04 + 199 x 50.80
        (day(27), D("100290.54")),  # 90,042.04 + 199 x 51.50
    ]


def test_enums_match_database_values() -> None:
    assert {s.value for s in OrderStatus} == {
        "working",
        "filled",
        "cancelled",
        "expired",
        "rejected",
    }
    assert {s.value for s in OrderSide} == {"buy", "sell"}
    assert {t.value for t in OrderType} == {"market", "limit", "stop"}
    assert {t.value for t in TimeInForce} == {"day", "gtc"}


async def test_signal_order_expires_if_entry_session_opened_before_submission(
    sessions: Sessions,
) -> None:
    """The entry bar is not stored yet when the (late) order is submitted, so the
    submission check cannot see it; processing must still refuse to fill."""
    signal_id, signal_bar, account_id = await signal_setup(sessions)
    too_late = signal_bar + timedelta(days=1, hours=12)  # entry session opened at +9h30m
    result = await service(sessions, too_late).submit_signal_order(
        account_id, SignalOrderRequest(signal_id=signal_id)
    )
    assert result.accepted

    entry = Bar(
        timestamp=signal_bar + timedelta(days=1),
        open=D(142),
        high=D("142.5"),
        low=D("141.5"),
        close=D(142),
        volume=D(1200),
    )
    await store(sessions, [entry], symbol="UP")
    processed = await service(sessions, entry.timestamp + timedelta(days=1, hours=1)).process(
        account_id
    )
    assert [(e.outcome, e.detail) for e in processed.events] == [
        ("expired", "submitted after the entry session had opened")
    ]


async def test_signal_order_never_fills_after_its_entry_session(sessions: Sessions) -> None:
    signal_id, signal_bar, account_id = await signal_setup(sessions)
    await service(sessions, signal_bar + timedelta(days=1, hours=2)).submit_signal_order(
        account_id, SignalOrderRequest(signal_id=signal_id)
    )
    entry_ts = signal_bar + timedelta(days=1)
    # Simulate the entry session having been processed already (e.g. its bar was
    # ingested and processed by an earlier run before this order existed).
    async with sessions.begin() as session:
        await session.execute(
            text("UPDATE paper_accounts SET processed_through = :ts WHERE id = :id"),
            {"ts": entry_ts, "id": account_id},
        )
    later = [
        Bar(timestamp=entry_ts, open=D(142), high=D(143), low=D(141), close=D(142), volume=D(1200)),
        Bar(
            timestamp=entry_ts + timedelta(days=1),
            open=D(142),
            high=D(143),
            low=D(141),
            close=D(142),
            volume=D(1200),
        ),
    ]
    await store(sessions, later, symbol="UP")
    result = await service(sessions, later[-1].timestamp + timedelta(days=1, hours=1)).process(
        account_id
    )
    assert [(e.outcome, e.detail) for e in result.events] == [
        ("expired", "entry session has passed")
    ]


async def test_market_exit_fills_at_the_open_before_an_intraday_stop(sessions: Sessions) -> None:
    await store(sessions, [*HISTORY, bar(26, "50", "51", "49.8", "50.8")])
    account_id = await new_account(sessions)
    await service(sessions, before_open(26)).submit_order(account_id, buy())
    await service(sessions, after_close(26)).process(account_id)
    await service(sessions, before_open(27)).submit_order(
        account_id, ManualOrderRequest(symbol=SYMBOL, side="sell")
    )
    # Opens at 51 (above the 48 stop), then trades down through it.
    await store(sessions, [bar(27, "51", "51.2", "47", "47.5")])
    await service(sessions, after_close(27)).process(account_id)

    (exit_fill, _entry) = await service(sessions, after_close(27)).list_fills(account_id, limit=5)
    assert (exit_fill.side, exit_fill.base_price, exit_fill.price) == ("sell", D(51), D("50.96"))
    stop = next(o for o in await orders(sessions, account_id) if o.order_type == "stop")
    assert (stop.status, stop.status_reason) == ("cancelled", "position closed")
