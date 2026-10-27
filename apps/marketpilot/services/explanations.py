"""Explanations of stored records (orders, signals, backtests), optionally AI-written.

Fact sheets are built here, deterministically, from what the database holds. The
AI layer (`packages.ai`) only rephrases them and is validated; every attempt is
stored in `ai_explanations`, including rejections and provider failures.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.ai.explanations import (
    DRAFT_SCHEMA,
    SYSTEM_PROMPT,
    Explanation,
    ExplanationDraft,
    Fact,
    FactSheet,
    accepted_explanation,
    deterministic_explanation,
    user_prompt,
    validate,
)
from packages.ai.model import AIError, ExplanationModel
from packages.database.models import (
    AIExplanationRecord,
    BacktestRecord,
    PaperOrderRecord,
    ResearchReportRecord,
    SignalRecord,
)
from packages.shared.errors import AppError
from packages.shared.timeutils import utc_now

logger = logging.getLogger(__name__)


class SubjectKind(StrEnum):
    ORDER = "order"
    SIGNAL = "signal"
    BACKTEST = "backtest"
    REPORT = "report"  # an AlphaScout research report


class ExplanationNotFoundError(AppError):
    http_status = 404
    error_type = "not_found"


class ExplanationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        model: ExplanationModel | None,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._model = model
        self._now = now

    async def explain(self, kind: SubjectKind, subject_id: int) -> Explanation:
        async with self._sessions() as session:
            sheet = await build_fact_sheet(session, kind, subject_id)

        record = AIExplanationRecord(
            subject_kind=kind.value,
            subject_id=subject_id,
            facts=sheet.as_payload(),
            facts_hash=sheet.fingerprint,
            issues=[],
            created_at=self._now(),
        )
        explanation = await self._attempt(sheet, record)
        async with self._sessions.begin() as session:
            session.add(record)
        logger.info(
            "explanation produced",
            extra={"subject": f"{kind.value}:{subject_id}", "status": record.status},
        )
        return explanation

    async def history(self, kind: SubjectKind, subject_id: int) -> list[AIExplanationRecord]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AIExplanationRecord)
                .where(
                    AIExplanationRecord.subject_kind == kind.value,
                    AIExplanationRecord.subject_id == subject_id,
                )
                .order_by(AIExplanationRecord.id.desc())
                .limit(50)
            )
            return list(rows.all())

    async def _attempt(self, sheet: FactSheet, record: AIExplanationRecord) -> Explanation:
        if self._model is None:
            record.status = "disabled"
            return deterministic_explanation(
                sheet, "AI explanations are not configured (ANTHROPIC_API_KEY is not set)."
            )
        record.provider = self._model.name
        try:
            reply = await self._model.generate(
                system=SYSTEM_PROMPT, user=user_prompt(sheet), schema=DRAFT_SCHEMA
            )
        except AIError as exc:
            record.status, record.error_type = "failed", exc.error_type
            record.issues = [exc.message]
            return deterministic_explanation(sheet, f"the AI call failed ({exc.message}).")
        record.model, record.request_id, record.draft = reply.model, reply.request_id, reply.data
        try:
            draft = ExplanationDraft.model_validate(reply.data)
        except ValidationError as exc:
            record.status = "rejected"
            record.issues = [f"answer does not match the schema: {exc.error_count()} error(s)"]
            return deterministic_explanation(sheet, "the AI answer did not match the schema.")
        issues = validate(draft, sheet)
        if issues:
            record.status, record.issues = "rejected", issues
            return deterministic_explanation(
                sheet, "the AI answer failed validation (" + "; ".join(issues[:3]) + ")."
            )
        record.status = "accepted"
        return accepted_explanation(draft, sheet, reply.model)


# --- fact sheets -------------------------------------------------------------------------


async def build_fact_sheet(session: AsyncSession, kind: SubjectKind, subject_id: int) -> FactSheet:
    builders = {
        SubjectKind.ORDER: (PaperOrderRecord, _order_sheet),
        SubjectKind.SIGNAL: (SignalRecord, _signal_sheet),
        SubjectKind.BACKTEST: (BacktestRecord, _backtest_sheet),
        SubjectKind.REPORT: (ResearchReportRecord, _report_sheet),
    }
    model, build = builders[kind]
    record = await session.get(model, subject_id)
    if record is None:
        raise ExplanationNotFoundError(f"{kind.value} {subject_id} not found")
    return build(record)


def _checks(prefix: str, checks: list[dict]) -> list[Fact]:
    return [
        Fact(
            f"{prefix}.{c['rule']}",
            f"rule {c['rule']}",
            f"{'passed' if c['passed'] else 'FAILED'}: {c['detail']}",
        )
        for c in checks
    ]


def _order_sheet(o: PaperOrderRecord) -> FactSheet:
    facts = [
        Fact("order.mode", "mode", "PAPER (simulated)"),
        Fact("order.symbol", "symbol", o.symbol),
        Fact("order.asset_class", "asset class", o.asset_class),
        Fact("order.side", "side", o.side),
        Fact("order.type", "order type", f"{o.order_type}, time in force {o.time_in_force}"),
        Fact("order.quantity", "quantity", str(o.quantity)),
        Fact("order.status", "status", o.status),
    ]
    if o.status_reason:
        facts.append(Fact("order.status_reason", "status reason", o.status_reason))
    for name in ("limit_price", "stop_price", "protective_stop_price", "reserved_cost"):
        value = getattr(o, name)
        if value is not None:
            facts.append(Fact(f"order.{name}", name.replace("_", " "), str(value)))
    if o.sizing:
        facts += [
            Fact(f"sizing.{k}", f"sizing {k.replace('_', ' ')}", str(v))
            for k, v in sorted(o.sizing.items())
            if not isinstance(v, dict | list)
        ]
    facts += _checks("check", o.risk_checks)
    return FactSheet(
        "order",
        o.id,
        f"PAPER {o.asset_class} {o.side} order {o.id} for {o.symbol}: {o.status}",
        facts,
        notes=["The risk gate evaluates every rule and reports all of them, not only failures."],
    )


def _signal_sheet(s: SignalRecord) -> FactSheet:
    facts = [
        Fact("signal.symbol", "symbol", s.symbol),
        Fact("signal.side", "side", s.side),
        Fact("signal.bar", "completed signal bar", s.signal_bar_at.isoformat()),
        Fact("signal.data", "data", f"{s.timeframe} bars, {s.feed} feed, {s.adjustment}"),
        Fact("signal.reference_price", "reference close", str(s.reference_price)),
        Fact("signal.stop_price", "protective stop", str(s.stop_price)),
        Fact("signal.risk_per_share", "risk per share", str(s.risk_per_share)),
        Fact("signal.atr", "ATR", str(s.atr)),
        Fact(
            "signal.entry_window",
            "entry window",
            f"next session only; cancelled if it opens below {s.cancel_if_open_below} "
            f"or above {s.cancel_if_open_above}",
        ),
        Fact(
            "signal.exits",
            "exit rules",
            f"protective stop; trailing exit on a {s.trailing_exit_lookback_bars}-bar low; "
            f"time stop after {s.time_stop_bars} bars",
        ),
    ]
    facts += [
        Fact(f"score.{k}", f"score component {k}", str(v))
        for k, v in sorted(s.score_components.items())
    ]
    facts += _checks("rule", s.rule_checks)
    return FactSheet(
        "signal",
        s.id,
        f"{s.side} signal {s.id} for {s.symbol}",
        facts,
        notes=[
            "A signal is a rule-based entry setup, not a recommendation.",
            "Score components are not probabilities.",
        ],
    )


def _backtest_sheet(b: BacktestRecord) -> FactSheet:
    report = b.report
    facts = [
        Fact("backtest.strategy", "strategy", f"{b.strategy} {b.strategy_version}"),
        Fact("backtest.bars_used", "bars used", str(b.bars_used)),
    ]
    facts += [
        Fact(f"metric.{k}", k.replace("_", " "), str(v))
        for k, v in sorted((report.get("metrics") or {}).items())
        if v is not None and not isinstance(v, dict | list)
    ]
    benchmark = report.get("benchmark") or {}
    facts += [
        Fact(f"benchmark.{k}", f"benchmark {k.replace('_', ' ')}", str(v))
        for k, v in sorted(benchmark.items())
        if v is not None and not isinstance(v, dict | list)
    ]
    facts += [
        Fact(f"warning.{i + 1}", "warning", w) for i, w in enumerate(report.get("warnings", []))
    ]
    return FactSheet(
        "backtest",
        b.id,
        f"Backtest {b.id} of {b.strategy} {b.strategy_version}",
        facts,
        notes=[
            "A backtest is hypothetical: past simulated results with assumed costs.",
            "It is not evidence of future profitability.",
        ],
    )


def _report_sheet(r: ResearchReportRecord) -> FactSheet:
    report = r.report
    facts = [
        # Company names and filing text come from outside sources: untrusted data.
        Fact("report.company", "company (as filed with the SEC)", str(report["company"])),
        Fact("report.symbol", "symbol", r.symbol),
        Fact("report.verdict", "verdict", r.verdict),
        Fact("report.verdict_reasons", "verdict reasons", "; ".join(report["verdict_reasons"])),
        Fact("report.score", "heuristic score (not a probability)", f"{report['score_total']}/100"),
    ]
    if r.reference_price is not None:
        facts.append(Fact("report.reference_price", "reference price", str(r.reference_price)))
    for e in report["evidence"]:
        if e["value"] is not None:
            facts.append(Fact(f"evidence.{e['key']}", e["claim"], str(e["value"])))
    valuation = report.get("valuation")
    if valuation:
        for s in valuation["scenarios"]:
            name = s["assumptions"]["name"]
            facts.append(
                Fact(
                    f"scenario.{name}",
                    f"{name} scenario over {valuation['horizon_months']} months",
                    f"implied price {s['implied_price']}, return {s['return_pct']}%, revenue "
                    f"CAGR {s['assumptions']['revenue_cagr']}, EV/Sales "
                    f"{s['assumptions']['ev_to_sales']}, dilution "
                    f"{s['assumptions']['annual_dilution']}/yr",
                )
            )
    for i, f in enumerate(report["red_team"]):
        facts.append(Fact(f"red_team.{i + 1}", f"red-team {f['severity']} finding", f["detail"]))
    return FactSheet(
        "report",
        r.id,
        f"AlphaScout research report {r.id} (version {r.version}) for {r.symbol}: {r.verdict}",
        facts,
        notes=[
            "Scenarios are hypothetical arithmetic on stated assumptions, not forecasts.",
            "A red-team hard finding rejects the thesis regardless of the score.",
        ],
    )
