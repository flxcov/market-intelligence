"""PAPER options against a real database: quotes in, fills, marks, expiry, reconciliation.

Synthetic data only. Calendar: June 2024 (U.S. Eastern = UTC-4), so daily bars are
stamped 04:00Z and the regular close is 20:00Z. The underlying OPTU closes at 100
every day unless a test says otherwise. Defaults: $100,000 account, 0.5% risk per
trade ($500 budget), $0.65 per contract, quotes on the indicative feed.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.execution.models import OrderStatus
from apps.marketpilot.services.alerts import AlertService
from apps.marketpilot.services.forward_trading import (
    CreateDeploymentRequest,
    ForwardTradingService,
    ShutdownRequest,
)
from apps.marketpilot.services.option_trading import OptionChainService, OptionTradingService
from apps.marketpilot.services.paper_models import CreateAccountRequest, OptionOrderRequest
from apps.marketpilot.services.paper_trading import PaperTradingService
from packages.database.market_bars import SeriesKey, upsert_bars
from packages.database.models import PaperLedgerRecord, PaperOrderRecord
from packages.database.option_quotes import insert_quotes, latest_chain
from packages.market_data.models import Adjustment, Bar, DataFeed
from packages.market_data.options import OptionChainRequest, OptionFeed, OptionQuote
from tests.fakes import FakeProvider

pytestmark = pytest.mark.anyio

D = Decimal
Sessions = async_sessionmaker[AsyncSession]
UNDERLYING = "OPTU"
CALL = "OPTU240621C00100000"
PUT = "OPTU240621P00100000"
EXPIRY_BAR = datetime(2024, 6, 21, 4, tzinfo=UTC)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2024, 6, day, hour, minute, tzinfo=UTC)


CREATED = at(3, 13)  # 09:00 Eastern
SUBMIT = at(3, 14)  # 10:00 Eastern


def bar(day: int, close: str = "100") -> Bar:
    c = D(close)
    return Bar(
        timestamp=datetime(2024, 6, day, 4, tzinfo=UTC),
        open=c,
        high=c + 1,
        low=c - 1,
        close=c,
        volume=D(1_000_000),
    )


def quote(when: datetime, bid="2.40", ask="2.50", bid_size=10, ask_size=10, symbol=CALL):
    return OptionQuote(
        symbol=symbol,
        quoted_at=when,
        bid=D(bid),
        ask=D(ask),
        bid_size=D(bid_size),
        ask_size=D(ask_size),
    )


async def store_bars(sessions: Sessions, bars: list[Bar]) -> None:
    async with sessions.begin() as session:
        await upsert_bars(
            session,
            SeriesKey(UNDERLYING, "1Day", DataFeed.IEX, Adjustment.RAW),
            bars,
            provider="synthetic",
            retrieved_at=at(25, 0),
        )


async def store_quotes(sessions: Sessions, *quotes: OptionQuote) -> None:
    async with sessions.begin() as session:
        await insert_quotes(
            session,
            list(quotes),
            underlying=UNDERLYING,
            feed=OptionFeed.INDICATIVE,
            provider="synthetic",
            retrieved_at=at(25, 0),
        )


def paper(sessions: Sessions, now: datetime) -> PaperTradingService:
    return PaperTradingService(sessions, default_feed=DataFeed.IEX, now=lambda: now)


def options(sessions: Sessions, now: datetime) -> OptionTradingService:
    return OptionTradingService(sessions, now=lambda: now)


async def new_account(sessions: Sessions) -> int:
    request = CreateAccountRequest(name="options", starting_cash=D(100_000))
    return (await paper(sessions, CREATED).create_account(request)).id


def buy(quantity=1, limit="2.50", **kwargs) -> OptionOrderRequest:
    order_type = "limit" if limit is not None else "market"
    return OptionOrderRequest(
        symbol=kwargs.pop("symbol", CALL),
        side="buy",
        quantity=quantity,
        order_type=order_type,
        limit_price=D(limit) if limit is not None else None,
        **kwargs,
    )


async def ledger(sessions: Sessions, account_id: int) -> list[PaperLedgerRecord]:
    async with sessions() as session:
        stmt = select(PaperLedgerRecord).where(PaperLedgerRecord.account_id == account_id)
        return list((await session.scalars(stmt.order_by(PaperLedgerRecord.id))).all())


async def order(sessions: Sessions, order_id: int) -> PaperOrderRecord:
    async with sessions() as session:
        return await session.get(PaperOrderRecord, order_id)


async def assert_balanced(sessions: Sessions, account_id: int, now: datetime) -> None:
    result = await paper(sessions, now).reconcile(account_id)
    assert result.balanced, [c for c in result.checks if not c.passed]


async def bought_one_call(sessions: Sessions) -> tuple[int, int]:
    """Account holding 1 CALL bought at the 2.50 ask at 15:00Z on June 3."""
    account_id = await new_account(sessions)
    await store_quotes(sessions, quote(at(3, 13, 50)))
    result = await options(sessions, SUBMIT).submit_order(account_id, buy())
    assert result.accepted, result.order.risk_checks
    await store_quotes(
        sessions,
        quote(at(3, 14, 30), bid="2.55", ask="2.60"),  # above the limit: no fill
        quote(at(3, 15), bid="2.45", ask="2.50", ask_size=1),
    )
    processed = await paper(sessions, at(3, 15, 30)).process(account_id)
    [event] = processed.events
    assert (event.outcome, event.session) == ("filled", at(3, 15))
    return account_id, result.order.id


# --- the main lifecycle -------------------------------------------------------------


async def test_buy_fills_at_the_ask_after_submission_and_is_marked_at_the_bid(
    sessions: Sessions,
) -> None:
    account_id, order_id = await bought_one_call(sessions)
    filled = await order(sessions, order_id)
    assert filled.status == OrderStatus.FILLED
    assert (filled.asset_class, filled.multiplier) == ("option", 100)
    assert filled.sizing["premium_at_risk"] == "250.65"
    assert filled.sizing["breakeven_at_expiry"] == "102.50"

    view = await paper(sessions, at(3, 15, 30)).account_view(account_id)
    assert view.cash == D("99749.35")  # 100000 - 1 x 100 x 2.50 - 0.65
    [position] = view.positions
    assert (position.symbol, position.quantity, position.multiplier) == (CALL, 1, 100)
    assert position.average_cost == D("2.5000")
    assert position.mark_price == D("2.45")  # latest bid
    assert position.mark_bar_at == at(3, 15)
    assert position.market_value == D("245.00")
    assert view.equity == D("99994.35")
    assert view.option_settings.quote_feed is OptionFeed.INDICATIVE

    fills = await paper(sessions, at(3, 15, 30)).list_fills(account_id, limit=10)
    assert [(f.price, f.multiplier, f.commission) for f in fills] == [(D("2.50"), 100, D("0.65"))]
    await assert_balanced(sessions, account_id, at(3, 15, 30))


async def test_expiry_in_the_money_settles_at_intrinsic_from_the_underlying_close(
    sessions: Sessions,
) -> None:
    account_id, _ = await bought_one_call(sessions)
    await store_bars(sessions, [bar(d) for d in range(3, 21)] + [bar(21, "105.25")])

    result = await paper(sessions, at(22, 5)).process(account_id)
    settled = [e for e in result.events if e.outcome == "settled"]
    assert len(settled) == 1
    assert settled[0].order_id is None and settled[0].symbol == CALL
    assert settled[0].session == at(21, 20)  # 16:00 Eastern on expiration day
    assert "in the money" in settled[0].detail

    view = await paper(sessions, at(22, 5)).account_view(account_id)
    assert view.cash == D("100274.35")  # 99749.35 + 5.25 x 100
    assert view.positions[0].quantity == 0
    assert view.realized_pnl == D("275.00")  # 525 - 250
    expire = (await ledger(sessions, account_id))[-1]
    assert (expire.kind, expire.amount, expire.price, expire.fill_id) == (
        "expire",
        D("525.00"),
        D("5.25"),
        None,
    )
    await assert_balanced(sessions, account_id, at(22, 5))

    # Idempotent: processing again settles nothing more.
    again = await paper(sessions, at(23, 5)).process(account_id)
    assert not [e for e in again.events if e.outcome == "settled"]
    assert len(await ledger(sessions, account_id)) == 4  # deposit, buy, fee, expire


async def test_expiry_out_of_the_money_loses_exactly_premium_plus_commission(
    sessions: Sessions,
) -> None:
    account_id, _ = await bought_one_call(sessions)
    await store_bars(sessions, [bar(d) for d in range(3, 21)] + [bar(21, "99.99")])
    await paper(sessions, at(22, 5)).process(account_id)
    view = await paper(sessions, at(22, 5)).account_view(account_id)
    assert view.cash == D("99749.35")
    assert view.equity - D(100_000) == D("-250.65")
    await assert_balanced(sessions, account_id, at(22, 5))


async def test_settlement_waits_for_the_underlying_bar_and_never_guesses(
    sessions: Sessions,
) -> None:
    account_id, _ = await bought_one_call(sessions)
    await store_bars(sessions, [bar(d) for d in range(3, 21)])  # no June 21 bar

    result = await paper(sessions, at(22, 5)).process(account_id)
    assert not [e for e in result.events if e.outcome == "settled"]
    assert any("cannot be settled yet" in n for n in result.notices)
    view = await paper(sessions, at(22, 5)).account_view(account_id)
    assert view.positions[0].quantity == 1
    assert any("awaiting settlement" in n for n in view.notices)

    alerts = await AlertService(
        sessions, default_feed=DataFeed.IEX, provider_configured=True, now=lambda: at(22, 5)
    ).evaluate()
    [alert] = [a for a in alerts.alerts if a.key.endswith("options_awaiting_settlement")]
    assert CALL in alert.detail

    await store_bars(sessions, [bar(21, "103")])
    # The June 21 session was not processed yet (processed_through is June 20).
    result = await paper(sessions, at(22, 6)).process(account_id)
    assert [e.outcome for e in result.events] == ["settled"]
    await assert_balanced(sessions, account_id, at(22, 6))


async def test_settlement_never_uses_an_expiration_bar_before_it_is_complete(
    sessions: Sessions,
) -> None:
    account_id, _ = await bought_one_call(sessions)
    # A June 21 bar ingested during that day could still change; it is complete only
    # one day after its timestamp (June 22 04:00Z).
    await store_bars(sessions, [bar(d) for d in range(3, 21)] + [bar(21, "104")])
    early = await paper(sessions, at(21, 21)).process(account_id)  # 17:00 Eastern
    assert not [e for e in early.events if e.outcome == "settled"]
    later = await paper(sessions, at(22, 5)).process(account_id)
    assert [e.outcome for e in later.events] == ["settled"]


async def test_sell_to_close_fills_at_the_bid(sessions: Sessions) -> None:
    account_id, _ = await bought_one_call(sessions)
    sell = OptionOrderRequest(symbol=CALL, side="sell", order_type="market")
    result = await options(sessions, at(3, 15, 40)).submit_order(account_id, sell)
    assert result.accepted and result.order.quantity == 1  # defaults to the whole position
    await store_quotes(
        sessions,
        quote(at(3, 15, 42), bid="3.20", ask="3.30", bid_size=0),  # nobody to sell to at that size
        quote(at(3, 15, 45), bid="3.10", ask="3.20", bid_size=5),
    )
    processed = await paper(sessions, at(3, 16)).process(account_id)
    assert [(e.outcome, e.session) for e in processed.events] == [("filled", at(3, 15, 45))]
    view = await paper(sessions, at(3, 16)).account_view(account_id)
    assert view.cash == D("100058.70")  # 99749.35 + 310 - 0.65
    assert view.realized_pnl == D("60.00")
    assert view.fees == D("1.30")
    await assert_balanced(sessions, account_id, at(3, 16))


# --- conservative fill rules ----------------------------------------------------------


async def test_quotes_before_submission_never_fill_and_day_orders_expire(
    sessions: Sessions,
) -> None:
    account_id = await new_account(sessions)
    await store_quotes(sessions, quote(at(3, 13, 55)), quote(at(3, 13, 59), bid="0.95", ask="1.00"))
    result = await options(sessions, SUBMIT).submit_order(account_id, buy())
    assert result.accepted
    processed = await paper(sessions, at(3, 19)).process(account_id)
    assert processed.events == []  # the cheap quote predates the order
    assert (await order(sessions, result.order.id)).status == OrderStatus.WORKING

    processed = await paper(sessions, at(3, 20, 30)).process(account_id)  # after 16:00 ET
    [event] = processed.events
    assert (event.outcome, event.session) == ("expired", at(3, 20))


async def test_displayed_size_must_cover_the_whole_order(sessions: Sessions) -> None:
    account_id = await new_account(sessions)
    await store_quotes(sessions, quote(at(3, 13, 55), bid="0.95", ask="1.00"))
    result = await options(sessions, SUBMIT).submit_order(account_id, buy(quantity=3, limit="1.00"))
    assert result.accepted
    await store_quotes(
        sessions,
        quote(at(3, 14, 10), bid="0.95", ask="1.00", ask_size=2),
        quote(at(3, 14, 20), bid="0.95", ask="1.00", ask_size=3),
    )
    processed = await paper(sessions, at(3, 15)).process(account_id)
    assert [(e.outcome, e.session) for e in processed.events] == [("filled", at(3, 14, 20))]


async def test_risk_is_rechecked_at_fill_with_the_actual_ask(sessions: Sessions) -> None:
    account_id = await new_account(sessions)
    await store_quotes(sessions, quote(at(3, 13, 55)))
    result = await options(sessions, SUBMIT).submit_order(account_id, buy(limit=None))
    assert result.accepted  # market order: 250.65 at the ask seen at submission
    await store_quotes(sessions, quote(at(3, 14, 30), bid="5.90", ask="6.00"))
    processed = await paper(sessions, at(3, 15)).process(account_id)
    [event] = processed.events
    assert event.outcome == "cancelled"
    assert "risk check failed at fill" in event.detail and "per_trade_risk" in event.detail
    assert (await paper(sessions, at(3, 15)).account_view(account_id)).cash == D(100_000)


# --- rejections at submission ---------------------------------------------------------


@pytest.mark.parametrize(
    ("quotes", "request_", "now", "rule"),
    [
        ([], buy(), SUBMIT, "market_data_available"),
        ([quote(at(3, 13, 40))], buy(), SUBMIT, "quote_fresh"),  # 20 minutes old
        ([quote(at(3, 13, 55), bid="2.00")], buy(), SUBMIT, "max_spread"),
        ([quote(at(3, 13, 55))], buy(quantity=2), SUBMIT, "per_trade_risk"),
        ([quote(at(3, 20, 10))], buy(), at(3, 20, 15), "day_order_session_open"),
        (
            [quote(at(21, 13, 55))],
            buy(time_in_force="gtc"),
            at(21, 14),
            "min_days_to_expiry",
        ),
        (
            [quote(at(3, 13, 55))],
            OptionOrderRequest(symbol=CALL, side="sell", quantity=1, order_type="market"),
            SUBMIT,
            "closes_existing_position",
        ),
    ],
)
async def test_unsafe_orders_are_rejected_and_recorded(
    sessions: Sessions, quotes, request_, now, rule
) -> None:
    account_id = await new_account(sessions)
    await store_quotes(sessions, *quotes)
    result = await options(sessions, now).submit_order(account_id, request_)
    assert not result.accepted
    assert result.order.status == OrderStatus.REJECTED
    failed = {c.rule for c in result.order.risk_checks if not c.passed}
    assert rule in failed, failed


async def test_client_order_id_makes_submission_idempotent(sessions: Sessions) -> None:
    account_id = await new_account(sessions)
    await store_quotes(sessions, quote(at(3, 13, 55)))
    first = await options(sessions, SUBMIT).submit_order(account_id, buy(client_order_id="c1"))
    again = await options(sessions, SUBMIT).submit_order(account_id, buy(client_order_id="c1"))
    assert again.already_existed and again.order.id == first.order.id


async def test_working_option_buy_reserves_cash_against_other_entries(
    sessions: Sessions,
) -> None:
    account_id = await new_account(sessions)
    await store_quotes(
        sessions, quote(at(3, 13, 55)), quote(at(3, 13, 55), symbol=PUT, bid="1.9", ask="2.0")
    )
    first = await options(sessions, SUBMIT).submit_order(account_id, buy())
    async with sessions() as session:
        stored = await session.get(PaperOrderRecord, first.order.id)
        assert stored.reserved_cost == D("250.65")
    second = await options(sessions, SUBMIT).submit_order(account_id, buy(symbol=PUT, limit="2.00"))
    cash = next(c for c in second.order.risk_checks if c.rule == "sufficient_cash")
    assert "available 99749.35" in cash.detail  # 100000 - the first order's 250.65
    exposure = next(c for c in second.order.risk_checks if c.rule == "max_gross_exposure")
    assert "exposure 450.65" in exposure.detail  # 250.65 reserved + 200 for the put
    assert second.accepted


# --- quote storage ----------------------------------------------------------------


async def test_chain_ingestion_is_idempotent_and_keeps_the_first_quote(
    sessions: Sessions,
) -> None:
    q1 = quote(at(3, 15))
    provider = FakeProvider(option_quotes=[q1, quote(at(3, 15), symbol=PUT)])
    service = OptionChainService(provider, sessions, default_feed=OptionFeed.INDICATIVE)
    request = OptionChainRequest(underlying=UNDERLYING)

    first = await service.ingest(request)
    assert (first.counts.inserted, first.counts.unchanged) == (2, 0)
    assert provider.option_requests[0].feed is OptionFeed.INDICATIVE
    again = await service.ingest(request)
    assert (again.counts.inserted, again.counts.unchanged, again.counts.conflicting) == (0, 2, 0)

    provider.option_quotes = [quote(at(3, 15), bid="2.41")]  # same key, different values
    changed = await service.ingest(request)
    assert changed.counts.conflicting == 1
    assert any("stored quote was kept" in n for n in changed.notices)

    provider.option_quotes = [quote(at(3, 16), bid="2.70", ask="2.80")]
    await service.ingest(request)
    async with sessions() as session:
        chain = await latest_chain(
            session, UNDERLYING, OptionFeed.INDICATIVE, at_or_before=at(3, 17)
        )
        earlier = await latest_chain(
            session, UNDERLYING, OptionFeed.INDICATIVE, at_or_before=at(3, 15, 30)
        )
    assert [(r.symbol, r.bid) for r in chain] == [(CALL, D("2.70")), (PUT, D("2.40"))]
    assert [(r.symbol, r.bid) for r in earlier] == [(CALL, D("2.40")), (PUT, D("2.40"))]
    assert chain[0].expiration == date(2024, 6, 21) and chain[0].strike == D(100)


# --- emergency shutdown ----------------------------------------------------------------


async def test_shutdown_liquidates_option_positions_with_option_sells(
    sessions: Sessions,
) -> None:
    account_id, _ = await bought_one_call(sessions)
    forward = ForwardTradingService(
        sessions, default_feed=DataFeed.IEX, provider=None, now=lambda: at(3, 15, 40)
    )
    deployment = await forward.create(
        CreateDeploymentRequest(account_id=account_id, name="d", symbols=["OPTU"])
    )
    result = await forward.shutdown(deployment.id, ShutdownRequest(liquidate=True))
    [liquidation] = result["liquidation_orders"]
    assert (liquidation["symbol"], liquidation["accepted"], liquidation["quantity"]) == (
        CALL,
        True,
        1,
    )
    await store_quotes(sessions, quote(at(3, 15, 45), bid="3.10", ask="3.20"))
    processed = await paper(sessions, at(3, 16)).process(account_id)
    assert [e.outcome for e in processed.events] == ["filled"]
    view = await paper(sessions, at(3, 16)).account_view(account_id)
    assert view.status == "halted" and view.positions[0].quantity == 0
    await assert_balanced(sessions, account_id, at(3, 16))
