import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.services.alerts import AlertService, Severity
from apps.marketpilot.services.forward_trading import (
    CreateDeploymentRequest,
    ForwardTradingService,
)
from apps.marketpilot.services.paper_common import add_ledger, load_portfolio, save_positions
from apps.marketpilot.services.paper_models import CreateAccountRequest
from packages.database.market_bars import SeriesKey, upsert_bars
from packages.database.models import (
    IngestionRunRecord,
    PaperAccountRecord,
    PaperEquitySnapshotRecord,
    TradingCycleRecord,
)
from packages.market_data.models import Adjustment, Bar, DataFeed
from tests.support import PROJECT_ROOT

pytestmark = pytest.mark.anyio

D = Decimal
Sessions = async_sessionmaker[AsyncSession]
NOW = datetime(2024, 6, 12, 10, tzinfo=UTC)  # a Wednesday morning


def service(sessions: Sessions, *, provider_configured: bool = True) -> AlertService:
    return AlertService(
        sessions,
        default_feed=DataFeed.IEX,
        provider_configured=provider_configured,
        now=lambda: NOW,
    )


async def keys(sessions: Sessions, **kwargs) -> dict[str, Severity]:
    report = await service(sessions, **kwargs).evaluate()
    return {a.key: a.severity for a in report.alerts}


async def account_and_deployment(sessions: Sessions, symbols=("AAA",)) -> tuple[int, int]:
    forward = ForwardTradingService(sessions, default_feed=DataFeed.IEX, now=lambda: NOW)
    account = await forward._paper.create_account(
        CreateAccountRequest(name="acct", starting_cash=D(100_000))
    )
    deployment = await forward.create(
        CreateDeploymentRequest(account_id=account.id, name="dep", symbols=list(symbols))
    )
    return account.id, deployment.id


async def fresh_bars(sessions: Sessions, symbol: str = "AAA") -> None:
    bar = Bar(
        timestamp=NOW.replace(hour=4) - timedelta(days=1),  # yesterday's bar, complete today
        open=D(10),
        high=D(11),
        low=D(9),
        close=D(10),
        volume=D(1000),
    )
    async with sessions.begin() as session:
        await upsert_bars(
            session,
            SeriesKey(symbol, "1Day", DataFeed.IEX, Adjustment.RAW),
            [bar],
            provider="synthetic",
            retrieved_at=NOW,
        )


async def add_cycle(
    sessions: Sessions, deployment_id: int, status: str, *, age=timedelta(hours=3), **extra
):
    async with sessions.begin() as session:
        session.add(
            TradingCycleRecord(
                deployment_id=deployment_id,
                as_of=NOW - age,
                status=status,
                started_at=NOW - age,
                finished_at=NOW - age,
                summary=extra.pop("summary", {}),
                **extra,
            )
        )


async def test_healthy_state_has_no_warnings(sessions: Sessions) -> None:
    _, deployment_id = await account_and_deployment(sessions)
    await fresh_bars(sessions)
    await add_cycle(sessions, deployment_id, "succeeded")
    report = await service(sessions).evaluate()
    assert report.alerts == []
    assert report.counts == {"info": 0, "warning": 0, "critical": 0}


async def test_new_setup_reports_info_only(sessions: Sessions) -> None:
    _, deployment_id = await account_and_deployment(sessions)
    await fresh_bars(sessions)
    found = await keys(sessions, provider_configured=False)
    assert found == {
        f"deployment:{deployment_id}:never_run": Severity.INFO,
        "system:provider_not_configured": Severity.INFO,
    }


@pytest.mark.parametrize(
    ("status", "severity"),
    [
        ("failed", Severity.CRITICAL),
        ("partial", Severity.WARNING),
        ("interrupted", Severity.WARNING),
    ],
)
async def test_problem_cycles_alert(sessions: Sessions, status: str, severity: Severity) -> None:
    _, deployment_id = await account_and_deployment(sessions)
    await fresh_bars(sessions)
    summary = (
        {"ingestion": [{"error": "provider_unavailable: down"}]} if status == "partial" else {}
    )
    await add_cycle(
        sessions, deployment_id, status, summary=summary, error_type="x", error_message="boom"
    )
    report = await service(sessions).evaluate()
    (alert,) = report.alerts
    assert (alert.key, alert.severity) == (
        f"deployment:{deployment_id}:last_cycle_{status}",
        severity,
    )
    assert ("down" in alert.detail) if status == "partial" else ("boom" in alert.detail)


async def test_no_recent_cycle_and_stale_data(sessions: Sessions) -> None:
    _, deployment_id = await account_and_deployment(sessions, symbols=("AAA", "BBB"))
    await fresh_bars(sessions, "AAA")  # BBB has no bars at all
    await add_cycle(sessions, deployment_id, "succeeded", age=timedelta(days=4))
    found = await keys(sessions)
    assert found == {
        f"deployment:{deployment_id}:no_recent_cycle": Severity.WARNING,
        f"deployment:{deployment_id}:stale_data:BBB": Severity.WARNING,
    }


async def test_stopped_deployments_are_quiet(sessions: Sessions) -> None:
    await account_and_deployment(sessions)
    async with sessions.begin() as session:
        await session.execute(text("UPDATE paper_deployments SET status = 'stopped'"))
    assert await keys(sessions) == {}


async def test_account_risk_and_reconciliation_alerts(sessions: Sessions) -> None:
    account_id, deployment_id = await account_and_deployment(sessions)
    await fresh_bars(sessions)
    await add_cycle(sessions, deployment_id, "succeeded")
    async with sessions.begin() as session:
        for days, equity in [(3, "120000"), (2, "103000"), (1, "100000")]:
            session.add(
                PaperEquitySnapshotRecord(
                    account_id=account_id,
                    bar_at=NOW - timedelta(days=days),
                    cash=D(equity),
                    market_value=D(0),
                    equity=D(equity),
                )
            )
        await session.execute(text("UPDATE paper_accounts SET status = 'halted', cash = cash + 5"))

    found = await keys(sessions)
    assert found == {
        f"account:{account_id}:drawdown_limit": Severity.CRITICAL,  # 120k peak -> ~100k: 16.7%
        f"account:{account_id}:reconciliation_failed": Severity.CRITICAL,
        f"account:{account_id}:daily_loss_limit": Severity.WARNING,  # 103k -> 100k: -2.9%
        f"account:{account_id}:halted": Severity.WARNING,
    }
    report = await service(sessions).evaluate()
    assert [a.severity for a in report.alerts][:2] == [Severity.CRITICAL, Severity.CRITICAL]


async def test_drawdown_near_limit_is_a_warning(sessions: Sessions) -> None:
    account_id, deployment_id = await account_and_deployment(sessions)
    await fresh_bars(sessions)
    await add_cycle(sessions, deployment_id, "succeeded")
    async with sessions.begin() as session:
        session.add(
            PaperEquitySnapshotRecord(
                account_id=account_id,
                bar_at=NOW - timedelta(days=5),
                cash=D(111_000),
                market_value=D(0),
                equity=D(111_000),
            )
        )
    found = await keys(sessions)
    assert found == {f"account:{account_id}:drawdown_near_limit": Severity.WARNING}  # 9.91%


async def test_recent_ingestion_failures(sessions: Sessions) -> None:
    async with sessions.begin() as session:
        for hours, symbol in [(30, "OLD"), (5, "AAA"), (1, "BBB")]:
            session.add(
                IngestionRunRecord(
                    provider="alpaca",
                    symbol=symbol,
                    timeframe="1Day",
                    adjustment="raw",
                    range_start=NOW,
                    range_end=NOW,
                    status="failed",
                    started_at=NOW - timedelta(hours=hours),
                    error_type="provider_unavailable",
                    error_message="timeout",
                )
            )
    report = await service(sessions).evaluate()
    (alert,) = report.alerts
    assert (
        alert.title == "2 failed ingestion(s) in the last 24 hours"
    )  # the 30h-old one is excluded
    assert alert.detail.startswith("latest: BBB")


def test_cli_alerts_exit_code(database_url: str, migrated_engine) -> None:
    """The scheduled-check contract: exit 2 when anything is critical."""
    env = {**os.environ, "DATABASE_URL": database_url, "LOG_LEVEL": "ERROR"}
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        env.pop(name, None)
    run = subprocess.run(
        [sys.executable, "-m", "apps.marketpilot.cli", "alerts"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert '"system:provider_not_configured"' in run.stdout

    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO paper_accounts (name, status, feed, starting_cash, cash, risk_limits, "
                "cost_model) VALUES ('x', 'active', 'iex', 100, 99, '{}', '{}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO paper_ledger (account_id, kind, amount, effective_at) "
                "VALUES (1, 'deposit', 100, now())"
            )
        )
    run = subprocess.run(
        [sys.executable, "-m", "apps.marketpilot.cli", "alerts"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 2  # cash 99 vs ledger 100: books do not reconcile
    assert "reconciliation_failed" in run.stdout


async def hold_shares(sessions: Sessions, account_id: int, symbol: str = "AAA") -> None:
    async with sessions.begin() as session:
        portfolio = await load_portfolio(session, account_id)
        lines = portfolio.buy(symbol, 10, D(100), D(0), NOW - timedelta(days=5))
        await add_ledger(session, account_id, lines)
        account = await session.get(PaperAccountRecord, account_id)
        account.cash = portfolio.cash
        await save_positions(session, account_id, portfolio)


async def store_series(sessions: Sessions, adjustment: Adjustment, closes: list[str]) -> None:
    bars = [
        Bar(
            timestamp=NOW.replace(hour=4) - timedelta(days=len(closes) - i),
            open=D(c),
            high=D(c),
            low=D(c),
            close=D(c),
            volume=D(1000),
        )
        for i, c in enumerate(closes)
    ]
    async with sessions.begin() as session:
        await upsert_bars(
            session,
            SeriesKey("AAA", "1Day", DataFeed.IEX, adjustment),
            bars,
            provider="synthetic",
            retrieved_at=NOW,
        )


async def test_split_in_a_held_stock_is_a_critical_alert(sessions: Sessions) -> None:
    account_id, _ = await account_and_deployment(sessions)
    await hold_shares(sessions, account_id)
    # A 2-for-1 split two days ago: raw halves, split-adjusted history was rescaled.
    await store_series(sessions, Adjustment.RAW, ["100", "101", "50.5", "51"])
    await store_series(sessions, Adjustment.SPLIT, ["50", "50.5", "50.5", "51"])

    report = await service(sessions).evaluate()
    [alert] = [a for a in report.alerts if "corporate_action" in a.key]
    assert alert.key == f"account:{account_id}:corporate_action:AAA"
    assert alert.severity is Severity.CRITICAL
    assert "from 2.0000 to 1.0000" in alert.detail


async def test_constant_adjustment_ratio_is_not_a_split(sessions: Sessions) -> None:
    account_id, _ = await account_and_deployment(sessions)
    await hold_shares(sessions, account_id)
    # Dividend-style tiny differences far below the tolerance, and no split.
    await store_series(sessions, Adjustment.RAW, ["100", "101", "102", "103"])
    await store_series(sessions, Adjustment.SPLIT, ["100", "101", "102", "103"])
    assert not [k for k in await keys(sessions) if "corporate_action" in k]
