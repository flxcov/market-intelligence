"""Forward PAPER trading deployments. Simulated only: nothing here reaches a broker."""

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Path, Query, Request

from apps.marketpilot.api.dependencies import get_app_settings, require_session_factory
from apps.marketpilot.api.security import require_write_access
from apps.marketpilot.config import Settings
from apps.marketpilot.services.forward_trading import (
    CreateDeploymentRequest,
    CycleView,
    DeploymentView,
    ForwardTradingService,
    RunCycleRequest,
    ShutdownRequest,
)

router = APIRouter(prefix="/paper/deployments", tags=["forward paper trading (simulated)"])
write = [Depends(require_write_access)]
DeploymentId = Annotated[int, Path(ge=1)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def _service(request: Request, settings: Settings) -> ForwardTradingService:
    return ForwardTradingService(
        require_session_factory(request),
        default_feed=settings.alpaca_data_feed,
        provider=request.app.state.market_data,
    )


@router.post("", dependencies=write)
async def create_deployment(
    request: Request, body: CreateDeploymentRequest, settings: SettingsDep
) -> DeploymentView:
    """Bind a strategy configuration and symbol list to a PAPER account."""
    return await _service(request, settings).create(body)


@router.get("")
async def list_deployments(request: Request, settings: SettingsDep) -> list[DeploymentView]:
    return await _service(request, settings).list_deployments()


@router.get("/{deployment_id}")
async def get_deployment(
    request: Request, deployment_id: DeploymentId, settings: SettingsDep
) -> DeploymentView:
    return await _service(request, settings).get(deployment_id)


@router.get("/{deployment_id}/cycles")
async def list_cycles(
    request: Request,
    deployment_id: DeploymentId,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 30,
) -> list[CycleView]:
    """Recent daily cycles, newest first, with what each step did."""
    return await _service(request, settings).cycles(deployment_id, limit=limit)


@router.post("/{deployment_id}/cycle", dependencies=write)
async def run_cycle(
    request: Request,
    deployment_id: DeploymentId,
    settings: SettingsDep,
    body: Annotated[RunCycleRequest | None, Body()] = None,
) -> CycleView:
    """Run one daily cycle now (normally triggered by the scheduler / CLI). Idempotent."""
    return await _service(request, settings).run_cycle(deployment_id, body or RunCycleRequest())


@router.post("/{deployment_id}/pause", dependencies=write)
async def pause(
    request: Request, deployment_id: DeploymentId, settings: SettingsDep
) -> DeploymentView:
    """Stop opening new positions; existing positions keep being managed."""
    return await _service(request, settings).set_status(deployment_id, "paused")


@router.post("/{deployment_id}/resume", dependencies=write)
async def resume(
    request: Request, deployment_id: DeploymentId, settings: SettingsDep
) -> DeploymentView:
    return await _service(request, settings).set_status(deployment_id, "active")


@router.post("/{deployment_id}/shutdown", dependencies=write)
async def shutdown(
    request: Request,
    deployment_id: DeploymentId,
    settings: SettingsDep,
    body: Annotated[ShutdownRequest | None, Body()] = None,
) -> dict[str, Any]:
    """Emergency stop: halts the whole PAPER account and stops the deployment for good.
    With `liquidate: true`, also submits market sells for every open position."""
    return await _service(request, settings).shutdown(deployment_id, body or ShutdownRequest())
