"""Option chain endpoints: ingest quotes from the provider, read stored quotes."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import AwareDatetime

from apps.marketpilot.api.dependencies import (
    get_app_settings,
    require_market_data_provider,
    require_session_factory,
)
from apps.marketpilot.api.schemas import (
    OptionChainIngestView,
    StoredOptionChain,
    StoredOptionQuote,
)
from apps.marketpilot.api.security import require_write_access
from apps.marketpilot.config import Settings
from apps.marketpilot.services.option_trading import OptionChainService, feed_notice, read_chain
from packages.database.option_quotes import quote_history
from packages.market_data.models import Symbol
from packages.market_data.options import (
    ContractSymbol,
    OptionChainRequest,
    OptionContract,
    OptionFeed,
)
from packages.shared.timeutils import utc_now

router = APIRouter(prefix="/market/options", tags=["options data"])
SettingsDep = Annotated[Settings, Depends(get_app_settings)]

_STORED_NOTICE = (
    "Stored quotes only: each contract shows its latest quote stored at or before `as_of`. "
    "Check `quoted_at` - an old quote says nothing about the current market."
)


@router.post("/chain/ingest", dependencies=[Depends(require_write_access)])
async def ingest_chain(
    request: Request, body: OptionChainRequest, settings: SettingsDep
) -> OptionChainIngestView:
    """Fetch the latest quote per contract from the provider and store it (idempotent).

    Requires `Authorization: Bearer <API_WRITE_TOKEN>`. Contracts without a usable
    quote, and non-standard contracts, are listed in `skipped`, never filled in.
    """
    service = OptionChainService(
        require_market_data_provider(request),
        require_session_factory(request),
        default_feed=settings.alpaca_options_feed,
    )
    result = await service.ingest(body)
    return OptionChainIngestView(
        underlying=result.underlying,
        feed=result.feed,
        provider=result.provider,
        retrieved_at=result.retrieved_at,
        received=result.received,
        inserted=result.counts.inserted,
        unchanged=result.counts.unchanged,
        conflicting=result.counts.conflicting,
        skipped=result.skipped,
        truncated=result.truncated,
        notices=result.notices,
    )


@router.get("/chain")
async def get_stored_chain(
    request: Request,
    settings: SettingsDep,
    underlying: Annotated[Symbol, Query()],
    feed: Annotated[OptionFeed | None, Query()] = None,
    expiration: Annotated[date | None, Query()] = None,
    as_of: Annotated[AwareDatetime | None, Query(description="Default: now")] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> StoredOptionChain:
    """The latest stored quote per contract. Makes no provider call."""
    feed = feed or settings.alpaca_options_feed
    at = as_of or utc_now()
    records = await read_chain(
        require_session_factory(request),
        underlying,
        feed,
        as_of=at,
        expiration=expiration,
        limit=limit,
    )
    notices = [_STORED_NOTICE, feed_notice(feed)]
    if not records:
        notices.append("No stored quotes match. Ingest with POST /market/options/chain/ingest.")
    return StoredOptionChain(
        underlying=underlying,
        feed=feed,
        as_of=at,
        quotes=[StoredOptionQuote.from_record(r) for r in records],
        notices=notices,
    )


@router.get("/contracts/{symbol}/quotes")
async def get_contract_quotes(
    request: Request,
    settings: SettingsDep,
    symbol: Annotated[ContractSymbol, Path()],
    feed: Annotated[OptionFeed | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> list[StoredOptionQuote]:
    """Every stored quote for one contract, oldest first (the most recent `limit`)."""
    contract = OptionContract.parse(symbol)
    sessions = require_session_factory(request)
    async with sessions() as session:
        records = await quote_history(
            session, contract.symbol, feed or settings.alpaca_options_feed, limit=limit
        )
    return [StoredOptionQuote.from_record(r) for r in records]
