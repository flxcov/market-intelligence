from typing import Annotated

from fastapi import APIRouter, Depends, Request

from apps.marketpilot.api.dependencies import get_app_settings, require_session_factory
from apps.marketpilot.config import Settings
from apps.marketpilot.services.alerts import AlertReport, AlertService

router = APIRouter(tags=["alerts"])


@router.get("/alerts")
async def alerts(
    request: Request, settings: Annotated[Settings, Depends(get_app_settings)]
) -> AlertReport:
    """Current operational alerts (failed cycles, stale data, risk limits, reconciliation)."""
    service = AlertService(
        require_session_factory(request),
        default_feed=settings.alpaca_data_feed,
        provider_configured=request.app.state.market_data is not None,
    )
    return await service.evaluate()
