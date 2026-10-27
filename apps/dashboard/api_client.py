"""A deliberately read-only client for the MarketPilot API.

It only issues GET requests and never sends the write token, so the dashboard
cannot place orders, change deployments or halt accounts: those stay with the
token-protected API and CLI. Calls never raise; they return an `ApiResult` so
pages can show a clear message instead of crashing when the API is down, the
database is not configured, or a resource is missing.
"""

import os
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_RESEARCH_API_URL = "http://127.0.0.1:8001"  # AlphaScout
TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class ApiResult:
    data: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class DashboardClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    @property
    def base_url(self) -> str:
        return str(self._http.base_url)

    def get(self, path: str, **params: Any) -> ApiResult:
        query = {k: v for k, v in params.items() if v is not None}
        try:
            response = self._http.get(path, params=query)
        except httpx.HTTPError as exc:
            return ApiResult(error=f"API not reachable at {self.base_url} ({type(exc).__name__})")
        try:
            body = response.json()
        except ValueError:
            return ApiResult(error=f"API returned non-JSON (HTTP {response.status_code})")
        if response.status_code >= 400:
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                return ApiResult(error=f"{error.get('type')}: {error.get('message')}")
            return ApiResult(error=f"HTTP {response.status_code}: {body}")
        return ApiResult(data=body)

    # Convenience wrappers: one per page need.
    def health(self) -> ApiResult:
        return self.get("/health")

    def alerts(self) -> ApiResult:
        return self.get("/alerts")

    def accounts(self) -> ApiResult:
        return self.get("/paper/accounts")

    def account(self, account_id: int) -> ApiResult:
        return self.get(f"/paper/accounts/{account_id}")

    def equity(self, account_id: int) -> ApiResult:
        return self.get(f"/paper/accounts/{account_id}/equity")

    def orders(self, account_id: int, status: str | None = None) -> ApiResult:
        return self.get(f"/paper/accounts/{account_id}/orders", status=status, limit=200)

    def fills(self, account_id: int) -> ApiResult:
        return self.get(f"/paper/accounts/{account_id}/fills", limit=200)

    def reconcile(self, account_id: int) -> ApiResult:
        return self.get(f"/paper/accounts/{account_id}/reconcile")

    def deployments(self) -> ApiResult:
        return self.get("/paper/deployments")

    def cycles(self, deployment_id: int) -> ApiResult:
        return self.get(f"/paper/deployments/{deployment_id}/cycles", limit=60)

    def signals(self, symbol: str | None = None) -> ApiResult:
        return self.get("/signals", symbol=symbol, limit=200)

    def backtests(self) -> ApiResult:
        return self.get("/backtests", limit=100)

    def backtest(self, backtest_id: int) -> ApiResult:
        return self.get(f"/backtests/{backtest_id}")

    def coverage(self) -> ApiResult:
        return self.get("/market/coverage")

    def ingestion_runs(self) -> ApiResult:
        return self.get("/market/ingestion-runs", limit=100)


def get_client() -> DashboardClient:
    """Client for the API at DASHBOARD_API_URL (default http://127.0.0.1:8000)."""
    base_url = os.environ.get("DASHBOARD_API_URL", DEFAULT_API_URL)
    return DashboardClient(httpx.Client(base_url=base_url, timeout=TIMEOUT_SECONDS))


def get_research_client() -> DashboardClient:
    """Client for the AlphaScout API at ALPHASCOUT_API_URL (default http://127.0.0.1:8001)."""
    base_url = os.environ.get("ALPHASCOUT_API_URL", DEFAULT_RESEARCH_API_URL)
    return DashboardClient(httpx.Client(base_url=base_url, timeout=TIMEOUT_SECONDS))
