"""PAPER trading endpoints. Simulated only: nothing here can reach a real broker."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Query, Request

from apps.marketpilot.api.dependencies import get_app_settings, require_session_factory
from apps.marketpilot.api.security import require_write_access
from apps.marketpilot.config import Settings
from apps.marketpilot.execution.models import OrderStatus
from apps.marketpilot.services.option_trading import OptionTradingService
from apps.marketpilot.services.paper_models import (
    AccountSummary,
    AccountView,
    CreateAccountRequest,
    EquitySnapshotView,
    FillView,
    LedgerEntryView,
    ManualOrderRequest,
    OptionOrderRequest,
    OrderResult,
    OrderView,
    ProcessRequest,
    ProcessResult,
    ReconcileResult,
    SignalOrderRequest,
)
from apps.marketpilot.services.paper_trading import PaperTradingService

router = APIRouter(prefix="/paper", tags=["paper trading (simulated)"])
write = [Depends(require_write_access)]
AccountId = Annotated[int, Path(ge=1)]
Limit = Annotated[int, Query(ge=1, le=1000)]


def _service(request: Request, settings: Settings) -> PaperTradingService:
    return PaperTradingService(
        require_session_factory(request),
        default_feed=settings.alpaca_data_feed,
        default_option_feed=settings.alpaca_options_feed,
    )


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


@router.post("/accounts", dependencies=write)
async def create_account(
    request: Request, body: CreateAccountRequest, settings: SettingsDep
) -> AccountView:
    """Create a PAPER account funded with simulated cash. Limits cannot be loosened later."""
    return await _service(request, settings).create_account(body)


@router.get("/accounts")
async def list_accounts(request: Request, settings: SettingsDep) -> list[AccountSummary]:
    return await _service(request, settings).list_accounts()


@router.get("/accounts/{account_id}")
async def get_account(
    request: Request, account_id: AccountId, settings: SettingsDep
) -> AccountView:
    """Cash, equity, positions (marked at the latest completed close), P&L and drawdown."""
    return await _service(request, settings).account_view(account_id)


@router.post("/accounts/{account_id}/orders", dependencies=write)
async def submit_order(
    request: Request, account_id: AccountId, body: ManualOrderRequest, settings: SettingsDep
) -> OrderResult:
    """Submit a PAPER order. It passes the risk gate now and again at fill time.

    Rejected orders are recorded too (`accepted: false`, with every rule check).
    """
    return await _service(request, settings).submit_order(account_id, body)


@router.post("/accounts/{account_id}/options/orders", dependencies=write)
async def submit_option_order(
    request: Request, account_id: AccountId, body: OptionOrderRequest
) -> OrderResult:
    """Submit a PAPER long option order (buy to open / sell to close; 100-share contracts).

    Fills come only from stored option quotes stamped after submission: buys at the
    ask, sells at the bid, and only if the displayed size covers the order. The
    response's `sizing` shows the premium at risk and the breakeven at expiry.
    """
    return await OptionTradingService(require_session_factory(request)).submit_order(
        account_id, body
    )


@router.post("/accounts/{account_id}/orders/from-signal", dependencies=write)
async def submit_signal_order(
    request: Request, account_id: AccountId, body: SignalOrderRequest, settings: SettingsDep
) -> OrderResult:
    """Turn a stored signal into a sized, risk-checked PAPER entry order (one per signal)."""
    return await _service(request, settings).submit_signal_order(account_id, body)


@router.get("/accounts/{account_id}/orders")
async def list_orders(
    request: Request,
    account_id: AccountId,
    settings: SettingsDep,
    status: Annotated[OrderStatus | None, Query()] = None,
    limit: Limit = 100,
) -> list[OrderView]:
    return await _service(request, settings).list_orders(account_id, status=status, limit=limit)


@router.post("/accounts/{account_id}/orders/{order_id}/cancel", dependencies=write)
async def cancel_order(
    request: Request,
    account_id: AccountId,
    order_id: Annotated[int, Path(ge=1)],
    settings: SettingsDep,
) -> OrderResult:
    return await _service(request, settings).cancel_order(account_id, order_id)


@router.post("/accounts/{account_id}/process", dependencies=write)
async def process(
    request: Request,
    account_id: AccountId,
    settings: SettingsDep,
    body: Annotated[ProcessRequest | None, Body()] = None,
) -> ProcessResult:
    """Advance the simulation through newly completed daily sessions (fills, stops, expiry)."""
    as_of = body.as_of if body else None
    return await _service(request, settings).process(account_id, as_of)


@router.get("/accounts/{account_id}/fills")
async def list_fills(
    request: Request, account_id: AccountId, settings: SettingsDep, limit: Limit = 100
) -> list[FillView]:
    return await _service(request, settings).list_fills(account_id, limit=limit)


@router.get("/accounts/{account_id}/ledger")
async def list_ledger(
    request: Request, account_id: AccountId, settings: SettingsDep, limit: Limit = 100
) -> list[LedgerEntryView]:
    return await _service(request, settings).list_ledger(account_id, limit=limit)


@router.get("/accounts/{account_id}/equity")
async def equity_history(
    request: Request, account_id: AccountId, settings: SettingsDep, limit: Limit = 1000
) -> list[EquitySnapshotView]:
    """Equity at each processed session close, oldest first."""
    return await _service(request, settings).equity_history(account_id, limit=limit)


@router.get("/accounts/{account_id}/reconcile")
async def reconcile(
    request: Request, account_id: AccountId, settings: SettingsDep
) -> ReconcileResult:
    """Re-derive cash and positions from the ledger and verify the accounting identity."""
    return await _service(request, settings).reconcile(account_id)


@router.post("/accounts/{account_id}/halt", dependencies=write)
async def halt(request: Request, account_id: AccountId, settings: SettingsDep) -> AccountView:
    """Emergency stop: cancel working buys and block new entries. Protective stops stay."""
    return await _service(request, settings).set_halted(account_id, True)


@router.post("/accounts/{account_id}/resume", dependencies=write)
async def resume(request: Request, account_id: AccountId, settings: SettingsDep) -> AccountView:
    return await _service(request, settings).set_halted(account_id, False)
