from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from apps.marketpilot.api.dependencies import get_app_settings, require_session_factory
from apps.marketpilot.api.security import require_write_access
from apps.marketpilot.config import Settings
from apps.marketpilot.services.backtests import (
    BacktestReport,
    BacktestRequest,
    BacktestService,
    BacktestSummary,
)

router = APIRouter(prefix="/backtests", tags=["backtests (hypothetical)"])


def _service(request: Request, settings: Settings) -> BacktestService:
    return BacktestService(require_session_factory(request), default_feed=settings.alpaca_data_feed)


@router.post("", dependencies=[Depends(require_write_access)])
async def run_backtest(
    request: Request,
    body: BacktestRequest,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> BacktestReport:
    """Run a backtest on STORED adjusted bars and save the report.

    Runs synchronously (up to 20 symbols, 15 years). Read the `warnings`:
    results are hypothetical and never evidence of future profitability.
    """
    return await _service(request, settings).run(body)


@router.get("")
async def list_backtests(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[BacktestSummary]:
    return await _service(request, settings).list(limit=limit)


@router.get("/{backtest_id}")
async def get_backtest(
    request: Request,
    backtest_id: Annotated[int, Path(ge=1)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> BacktestReport:
    return await _service(request, settings).get(backtest_id)
