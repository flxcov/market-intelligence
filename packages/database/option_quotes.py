"""Repository for stored option quotes.

Quotes are an append-only time series per (contract, feed). The natural key is
(symbol, feed, quoted_at): re-ingesting a snapshot whose latest quote has not
changed inserts nothing. A provider sending different values for the same
contract and quote time is kept as first stored and counted as a conflict
(a quote, once used for a simulated fill, must not silently change).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import OptionQuoteRecord
from packages.market_data.options import OptionFeed, OptionQuote

UPSERT_BATCH_SIZE = 500
_GREEKS = ("delta", "gamma", "theta", "vega", "rho")
_COMPARED = ("bid", "ask", "bid_size", "ask_size")


@dataclass(frozen=True)
class QuoteWriteCounts:
    inserted: int = 0
    unchanged: int = 0
    conflicting: int = 0


async def insert_quotes(
    session: AsyncSession,
    quotes: Sequence[OptionQuote],
    *,
    underlying: str,
    feed: OptionFeed,
    provider: str,
    retrieved_at: datetime,
) -> QuoteWriteCounts:
    """Store new quotes. Runs inside the caller's transaction; does not commit."""
    inserted = unchanged = conflicting = 0
    unique = {(q.symbol, q.quoted_at): q for q in quotes}  # one row per key per statement
    ordered = list(unique.values())
    for offset in range(0, len(ordered), UPSERT_BATCH_SIZE):
        batch = ordered[offset : offset + UPSERT_BATCH_SIZE]
        rows = [_row(q, underlying, feed, provider, retrieved_at) for q in batch]
        stmt = (
            insert(OptionQuoteRecord)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_option_quotes_symbol_feed_time")
            .returning(OptionQuoteRecord.symbol, OptionQuoteRecord.quoted_at)
        )
        written = {(r.symbol, r.quoted_at) for r in (await session.execute(stmt)).all()}
        inserted += len(written)
        existing_keys = [q for q in batch if (q.symbol, q.quoted_at) not in written]
        for quote in existing_keys:
            stored = await session.scalar(
                select(OptionQuoteRecord).where(
                    OptionQuoteRecord.symbol == quote.symbol,
                    OptionQuoteRecord.feed == feed.value,
                    OptionQuoteRecord.quoted_at == quote.quoted_at,
                )
            )
            same = stored is not None and all(
                getattr(stored, name) == getattr(quote, name) for name in _COMPARED
            )
            if same:
                unchanged += 1
            else:
                conflicting += 1
    return QuoteWriteCounts(inserted, unchanged, conflicting)


async def latest_quote(
    session: AsyncSession, symbol: str, feed: OptionFeed, *, at_or_before: datetime
) -> OptionQuoteRecord | None:
    return await session.scalar(
        select(OptionQuoteRecord)
        .where(
            OptionQuoteRecord.symbol == symbol,
            OptionQuoteRecord.feed == feed.value,
            OptionQuoteRecord.quoted_at <= at_or_before,
        )
        .order_by(OptionQuoteRecord.quoted_at.desc())
        .limit(1)
    )


async def quotes_between(
    session: AsyncSession, symbol: str, feed: OptionFeed, *, after: datetime, before: datetime
) -> list[OptionQuoteRecord]:
    """Quotes with after < quoted_at < before, oldest first."""
    rows = await session.scalars(
        select(OptionQuoteRecord)
        .where(
            OptionQuoteRecord.symbol == symbol,
            OptionQuoteRecord.feed == feed.value,
            OptionQuoteRecord.quoted_at > after,
            OptionQuoteRecord.quoted_at < before,
        )
        .order_by(OptionQuoteRecord.quoted_at)
    )
    return list(rows.all())


async def quote_history(
    session: AsyncSession, symbol: str, feed: OptionFeed, *, limit: int
) -> list[OptionQuoteRecord]:
    """The most recent `limit` quotes for one contract, oldest first."""
    rows = await session.scalars(
        select(OptionQuoteRecord)
        .where(OptionQuoteRecord.symbol == symbol, OptionQuoteRecord.feed == feed.value)
        .order_by(OptionQuoteRecord.quoted_at.desc())
        .limit(limit)
    )
    return list(reversed(rows.all()))


async def latest_chain(
    session: AsyncSession,
    underlying: str,
    feed: OptionFeed,
    *,
    at_or_before: datetime,
    expiration: date | None = None,
    limit: int = 1000,
) -> list[OptionQuoteRecord]:
    """The latest stored quote per contract, ordered by expiration, type and strike."""
    stmt = (
        select(OptionQuoteRecord)
        .where(
            OptionQuoteRecord.underlying == underlying,
            OptionQuoteRecord.feed == feed.value,
            OptionQuoteRecord.quoted_at <= at_or_before,
        )
        .ext(distinct_on(OptionQuoteRecord.symbol))
        .order_by(OptionQuoteRecord.symbol, OptionQuoteRecord.quoted_at.desc())
    )
    if expiration is not None:
        stmt = stmt.where(OptionQuoteRecord.expiration == expiration)
    rows = list((await session.scalars(stmt)).all())
    rows.sort(key=lambda r: (r.expiration, r.option_type, r.strike))
    return rows[:limit]


def _row(
    quote: OptionQuote, underlying: str, feed: OptionFeed, provider: str, retrieved_at: datetime
) -> dict:
    contract = quote.contract
    greeks = {
        name: str(getattr(quote, name)) for name in _GREEKS if getattr(quote, name) is not None
    }
    return {
        "symbol": quote.symbol,
        "underlying": underlying,
        "option_type": contract.option_type.value,
        "strike": contract.strike,
        "expiration": contract.expiration,
        "provider": provider,
        "feed": feed.value,
        "quoted_at": quote.quoted_at,
        "bid": quote.bid,
        "ask": quote.ask,
        "bid_size": quote.bid_size,
        "ask_size": quote.ask_size,
        "last_trade_price": quote.last_trade_price,
        "last_trade_at": quote.last_trade_at,
        "implied_volatility": quote.implied_volatility,
        "greeks": greeks or None,
        "retrieved_at": retrieved_at,
    }
