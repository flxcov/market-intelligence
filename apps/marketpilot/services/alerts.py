"""Operational alerts, computed from current state (not stored, so never duplicated).

Each alert has a stable `key`, so a dashboard or a scheduled check sees the same
alert until the underlying condition clears. Severities:

  critical  something is wrong or trading is blocked (failed cycle, books do not
            reconcile, drawdown limit reached)
  warning   needs attention soon (partial/interrupted cycle, stale data, no
            recent cycle, close to a risk limit, ingestion failures)
  info      noteworthy state (deployment never run, provider not configured)

Thresholds are calendar-day approximations (no exchange calendar yet), chosen
to stay quiet over a normal weekend plus one holiday.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.services.paper_common import option_contract
from apps.marketpilot.services.paper_trading import PaperTradingService
from packages.database.market_bars import SeriesKey, coverage, get_latest_bars
from packages.database.models import (
    IngestionRunRecord,
    IngestionStatus,
    PaperAccountRecord,
    PaperDeploymentRecord,
    PaperEquitySnapshotRecord,
    TradingCycleRecord,
)
from packages.market_data.models import Adjustment, DataFeed
from packages.shared.errors import AppError
from packages.shared.timeutils import utc_now

MAX_DAYS_WITHOUT_CYCLE = 3
MAX_DATA_AGE_DAYS = 4  # from the end of the latest completed daily bar
INGESTION_LOOKBACK = timedelta(hours=24)
NEAR_LIMIT_FRACTION = Decimal("0.8")


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


_RANK = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


class Alert(BaseModel):
    key: str
    severity: Severity
    subject: str
    title: str
    detail: str
    since: datetime | None = None


class AlertReport(BaseModel):
    generated_at: datetime
    counts: dict[str, int]
    alerts: list[Alert]


class AlertService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        default_feed: DataFeed,
        provider_configured: bool,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._provider_configured = provider_configured
        self._now = now
        self._paper = PaperTradingService(session_factory, default_feed=default_feed, now=now)

    async def evaluate(self) -> AlertReport:
        now = self._now()
        alerts: list[Alert] = []
        if not self._provider_configured:
            alerts.append(
                Alert(
                    key="system:provider_not_configured",
                    severity=Severity.INFO,
                    subject="system",
                    title="Market data provider not configured",
                    detail="Cycles will run on stored bars only. Set ALPACA_* in .env.",
                )
            )
        async with self._sessions() as session:
            deployments = (
                await session.scalars(select(PaperDeploymentRecord).order_by("id"))
            ).all()
            accounts = (await session.scalars(select(PaperAccountRecord).order_by("id"))).all()
            series = {(c.symbol, c.feed, c.adjustment): c for c in await coverage(session)}
            for deployment in deployments:
                alerts += await self._deployment_alerts(session, deployment, series, now)
            alerts += await self._ingestion_alerts(session, now)
            for account in accounts:
                alerts += await self._account_alerts(session, account)
        alerts.sort(key=lambda a: (_RANK[a.severity], a.key))
        counts = {s.value: sum(1 for a in alerts if a.severity is s) for s in Severity}
        return AlertReport(generated_at=now, counts=counts, alerts=alerts)

    async def _deployment_alerts(self, session, deployment, series, now) -> list[Alert]:
        if deployment.status == "stopped":
            return []
        subject = f"deployment {deployment.id} ({deployment.name})"
        prefix = f"deployment:{deployment.id}"
        alerts = []
        last = await session.scalar(
            select(TradingCycleRecord)
            .where(TradingCycleRecord.deployment_id == deployment.id)
            .order_by(TradingCycleRecord.id.desc())
            .limit(1)
        )
        if last is None:
            alerts.append(
                Alert(
                    key=f"{prefix}:never_run",
                    severity=Severity.INFO,
                    subject=subject,
                    title="Deployment has never run a cycle",
                    detail="Schedule `python -m apps.marketpilot.cli run-due` (see README).",
                )
            )
        else:
            if last.status == "failed":
                alerts.append(
                    Alert(
                        key=f"{prefix}:last_cycle_failed",
                        severity=Severity.CRITICAL,
                        subject=subject,
                        title="Last trading cycle failed",
                        detail=f"{last.error_type}: {last.error_message}",
                        since=last.started_at,
                    )
                )
            elif last.status in ("partial", "interrupted"):
                errors = [i["error"] for i in last.summary.get("ingestion", []) if "error" in i]
                alerts.append(
                    Alert(
                        key=f"{prefix}:last_cycle_{last.status}",
                        severity=Severity.WARNING,
                        subject=subject,
                        title=f"Last trading cycle {last.status}",
                        detail="; ".join(errors[:3]) or (last.error_message or ""),
                        since=last.started_at,
                    )
                )
            if now - last.started_at > timedelta(days=MAX_DAYS_WITHOUT_CYCLE):
                alerts.append(
                    Alert(
                        key=f"{prefix}:no_recent_cycle",
                        severity=Severity.WARNING,
                        subject=subject,
                        title=f"No cycle for more than {MAX_DAYS_WITHOUT_CYCLE} days",
                        detail="Is the scheduler (cron / Task Scheduler) still running?",
                        since=last.started_at,
                    )
                )
        account = await session.get(PaperAccountRecord, deployment.account_id)
        for symbol in deployment.symbols:
            stored = series.get((symbol, account.feed, "raw"))
            if stored is None:
                age_detail, since = "no raw bars stored", None
            else:
                age = now - (stored.last_bar + timedelta(days=1))
                if age <= timedelta(days=MAX_DATA_AGE_DAYS):
                    continue
                age_detail = f"latest raw bar {stored.last_bar.date()} ({age.days} days old)"
                since = stored.last_bar
            alerts.append(
                Alert(
                    key=f"{prefix}:stale_data:{symbol}",
                    severity=Severity.WARNING,
                    subject=subject,
                    title=f"Stale market data for {symbol}",
                    detail=f"{age_detail}; the strategy will refuse to act on it.",
                    since=since,
                )
            )
        return alerts

    async def _ingestion_alerts(self, session, now) -> list[Alert]:
        failures = (
            await session.scalars(
                select(IngestionRunRecord)
                .where(
                    IngestionRunRecord.status == IngestionStatus.FAILED,
                    IngestionRunRecord.started_at >= now - INGESTION_LOOKBACK,
                )
                .order_by(IngestionRunRecord.id.desc())
            )
        ).all()
        if not failures:
            return []
        latest = failures[0]
        return [
            Alert(
                key="system:ingestion_failures",
                severity=Severity.WARNING,
                subject="market data",
                title=f"{len(failures)} failed ingestion(s) in the last 24 hours",
                detail=f"latest: {latest.symbol} {latest.error_type}: {latest.error_message}",
                since=failures[-1].started_at,
            )
        ]

    async def _corporate_action_alerts(self, session, account, view, prefix, subject):
        """Flag held shares whose stored raw and split-adjusted closes stop being in a
        constant ratio: a split (or similar action) happened, and PAPER positions are not
        adjusted for it, so their marks and P&L are wrong until handled."""
        alerts = []
        feed = DataFeed(account.feed)
        for position in view.positions:
            if not position.quantity or option_contract(position.symbol) is not None:
                continue
            ratios = await split_ratio_changes(session, position.symbol, feed, self._now())
            if ratios:
                before, after, at = ratios
                alerts.append(
                    Alert(
                        key=f"{prefix}:corporate_action:{position.symbol}",
                        severity=Severity.CRITICAL,
                        subject=subject,
                        title=f"Possible split in held {position.symbol}: P&L is misstated",
                        detail=(
                            f"raw/split-adjusted close ratio changed from {before} to {after} "
                            f"at the bar of {at.date()}. PAPER positions are not adjusted for "
                            "corporate actions; review the position manually."
                        ),
                        since=at,
                    )
                )
        return alerts

    async def _account_alerts(self, session, account) -> list[Alert]:
        subject = f"PAPER account {account.id} ({account.name})"
        prefix = f"account:{account.id}"
        alerts = []
        if account.status == "halted":
            alerts.append(
                Alert(
                    key=f"{prefix}:halted",
                    severity=Severity.WARNING,
                    subject=subject,
                    title="Account halted",
                    detail="New entries are blocked; protective stops stay active.",
                )
            )
        try:
            reconcile = await self._paper.reconcile(account.id)
            view = await self._paper.account_view(account.id)
        except AppError as exc:
            return [
                *alerts,
                Alert(
                    key=f"{prefix}:cannot_value",
                    severity=Severity.CRITICAL,
                    subject=subject,
                    title="Account cannot be valued",
                    detail=exc.message,
                ),
            ]
        if not reconcile.balanced:
            failed = [f"{c.rule}: {c.detail}" for c in reconcile.checks if not c.passed]
            alerts.append(
                Alert(
                    key=f"{prefix}:reconciliation_failed",
                    severity=Severity.CRITICAL,
                    subject=subject,
                    title="Books do not reconcile",
                    detail="; ".join(failed),
                )
            )
        limit = view.risk_limits.max_drawdown_pct
        if view.drawdown_pct >= limit:
            alerts.append(
                Alert(
                    key=f"{prefix}:drawdown_limit",
                    severity=Severity.CRITICAL,
                    subject=subject,
                    title="Drawdown limit reached: new entries blocked",
                    detail=f"drawdown {view.drawdown_pct}% vs limit {limit}%",
                )
            )
        elif view.drawdown_pct >= limit * NEAR_LIMIT_FRACTION:
            alerts.append(
                Alert(
                    key=f"{prefix}:drawdown_near_limit",
                    severity=Severity.WARNING,
                    subject=subject,
                    title="Drawdown close to its limit",
                    detail=f"drawdown {view.drawdown_pct}% vs limit {limit}%",
                )
            )
        alerts += await self._corporate_action_alerts(session, account, view, prefix, subject)
        unsettled = [
            p.symbol
            for p in view.positions
            if p.quantity
            and (contract := option_contract(p.symbol)) is not None
            and contract.expires_at < self._now()
        ]
        if unsettled:
            alerts.append(
                Alert(
                    key=f"{prefix}:options_awaiting_settlement",
                    severity=Severity.WARNING,
                    subject=subject,
                    title="Expired option positions awaiting settlement",
                    detail=(
                        f"{', '.join(unsettled)}: ingest the underlying's raw daily bar for the "
                        "expiration date, then process the account."
                    ),
                )
            )
        last_two = (
            await session.scalars(
                select(PaperEquitySnapshotRecord)
                .where(PaperEquitySnapshotRecord.account_id == account.id)
                .order_by(PaperEquitySnapshotRecord.bar_at.desc())
                .limit(2)
            )
        ).all()
        previous = last_two[1].equity if len(last_two) == 2 else account.starting_cash
        if last_two and previous:
            change = (last_two[0].equity - previous) / previous * 100
            if change <= -view.risk_limits.max_daily_loss_pct:
                alerts.append(
                    Alert(
                        key=f"{prefix}:daily_loss_limit",
                        severity=Severity.WARNING,
                        subject=subject,
                        title="Daily loss limit hit: new entries blocked next session",
                        detail=(
                            f"last session {change:.2f}% vs limit "
                            f"-{view.risk_limits.max_daily_loss_pct}%"
                        ),
                        since=last_two[0].bar_at,
                    )
                )
        return alerts


SPLIT_CHECK_BARS = 60
_RATIO_TOLERANCE = Decimal("0.005")  # 0.5%: well above rounding noise in adjusted prices


async def split_ratio_changes(
    session: AsyncSession, symbol: str, feed: DataFeed, as_of: datetime
) -> tuple[Decimal, Decimal, datetime] | None:
    """(old ratio, new ratio, first bar with the new ratio) if raw / split-adjusted closes
    changed ratio within the last bars stored for both series, else None. Needs both
    series ingested AFTER the event (the provider adjusts history retroactively)."""
    closes = {}
    for adjustment in (Adjustment.RAW, Adjustment.SPLIT):
        bars = await get_latest_bars(
            session,
            SeriesKey(symbol, "1Day", feed, adjustment),
            end=as_of,
            limit=SPLIT_CHECK_BARS,
        )
        closes[adjustment] = {b.timestamp: b.close for b in bars}
    common = sorted(set(closes[Adjustment.RAW]) & set(closes[Adjustment.SPLIT]))
    previous = None
    for ts in common:
        ratio = closes[Adjustment.RAW][ts] / closes[Adjustment.SPLIT][ts]
        if previous is not None and abs(ratio - previous) > previous * _RATIO_TOLERANCE:
            return previous.quantize(Decimal("0.0001")), ratio.quantize(Decimal("0.0001")), ts
        previous = ratio
    return None
