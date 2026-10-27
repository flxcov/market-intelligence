from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from apps.marketpilot.api.dependencies import get_app_settings, require_session_factory
from apps.marketpilot.api.schemas import StoredSignal
from apps.marketpilot.api.security import require_write_access
from apps.marketpilot.config import Settings
from apps.marketpilot.services.signals import (
    SignalGenerationRequest,
    SignalGenerationResult,
    SignalService,
)
from packages.database import signals as signal_repo
from packages.market_data.models import Symbol

router = APIRouter(prefix="/signals", tags=["signals"])


@router.post("/generate", dependencies=[Depends(require_write_access)])
async def generate_signals(
    request: Request,
    body: SignalGenerationRequest,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> SignalGenerationResult:
    """Evaluate the strategy on STORED completed bars and record any signals.

    Returns every rule check for every symbol, including "no signal" and
    data-quality outcomes. Re-running for the same bar does not duplicate signals.
    Signals are research output for paper trading only; scores are not probabilities.
    Requires `Authorization: Bearer <API_WRITE_TOKEN>`.
    """
    service = SignalService(
        require_session_factory(request), default_feed=settings.alpaca_data_feed
    )
    return await service.generate(body)


@router.get("")
async def list_signals(
    request: Request,
    symbol: Annotated[Symbol | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[StoredSignal]:
    """Stored signals, most recent signal bar first."""
    sessions = require_session_factory(request)
    async with sessions() as session:
        rows = await signal_repo.list_signals(session, symbol=symbol, limit=limit)
    return [
        StoredSignal(
            id=signal.id,
            strategy=config.name,
            strategy_version=config.version,
            parameters_hash=config.parameters_hash,
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            feed=signal.feed,
            adjustment=signal.adjustment,
            side=signal.side,
            status=signal.status,
            signal_bar_at=signal.signal_bar_at,
            evaluated_at=signal.evaluated_at,
            data_retrieved_at=signal.data_retrieved_at,
            created_at=signal.created_at,
            reference_price=signal.reference_price,
            stop_price=signal.stop_price,
            cancel_if_open_below=signal.cancel_if_open_below,
            cancel_if_open_above=signal.cancel_if_open_above,
            expires_after_bars=signal.expires_after_bars,
            trailing_exit_lookback_bars=signal.trailing_exit_lookback_bars,
            time_stop_bars=signal.time_stop_bars,
            atr=signal.atr,
            risk_per_share=signal.risk_per_share,
            score_components=signal.score_components,
            rule_checks=signal.rule_checks,
        )
        for signal, config in rows
    ]
