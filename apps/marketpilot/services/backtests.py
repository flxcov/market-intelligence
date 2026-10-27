"""Run backtests on STORED bars and keep a reproducible record of each run.

Price series: backtests use ADJUSTED bars (default "all": splits and dividends)
for both signals and fills. That keeps positions correct across splits and
makes the benchmark comparison a total-return comparison. It also means fill
prices before a split/dividend are not the prices that actually traded then.
Raw bars are rejected.

Every report carries warnings about what the numbers can and cannot support.
Results are hypothetical and are never evidence that a strategy will profit.
"""

import asyncio
import hashlib
import logging
import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.backtesting.engine import BacktestEngine, BacktestRun
from apps.marketpilot.backtesting.metrics import (
    ClosedTrade,
    EquityPoint,
    PerformanceMetrics,
    compute_metrics,
    max_drawdown,
)
from apps.marketpilot.execution.costs import CostModel
from apps.marketpilot.risk.limits import RiskLimits
from apps.marketpilot.strategies.trend_following import (
    TrendFollowingParams,
    TrendFollowingStrategy,
)
from packages.database.market_bars import SeriesKey, get_bars_after
from packages.database.models import BacktestRecord
from packages.market_data.models import Adjustment, Bar, DataFeed, Symbol
from packages.shared.errors import AppError
from packages.shared.timeutils import parse_utc_bound, utc_now

logger = logging.getLogger(__name__)

MAX_SYMBOLS = 20
MAX_SPAN = timedelta(days=366 * 15)
MIN_TRADES_FOR_CONFIDENCE = 30
DISCLAIMER = (
    "Hypothetical backtest on historical data with assumed costs. It is not a "
    "prediction, not investment advice, and not evidence of future profitability."
)


class BacktestRequestError(AppError):
    http_status = 400
    error_type = "invalid_backtest_request"


class BacktestNotFoundError(AppError):
    http_status = 404
    error_type = "not_found"


def _dedupe(symbols: list[str]) -> list[str]:
    return list(dict.fromkeys(symbols))


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["trend_following"] = "trend_following"
    symbols: Annotated[
        list[Symbol], Field(min_length=1, max_length=MAX_SYMBOLS), AfterValidator(_dedupe)
    ]
    start: datetime = Field(description="First trading session (YYYY-MM-DD = 00:00 UTC).")
    end: datetime = Field(description="Last session (YYYY-MM-DD = 23:59:59 UTC).")
    starting_cash: Decimal = Field(default=Decimal(100_000), gt=0, le=Decimal(1_000_000_000))
    feed: DataFeed | None = None
    adjustment: Adjustment = Field(
        default=Adjustment.ALL, description="'all' or 'split'. Raw prices are not allowed."
    )
    parameters: TrendFollowingParams = Field(default_factory=TrendFollowingParams)
    risk_limits: RiskLimits = Field(default_factory=RiskLimits)
    cost_model: CostModel = Field(default_factory=CostModel)
    out_of_sample_start: datetime | None = Field(
        default=None, description="Report metrics separately before/after this date."
    )
    benchmark: Symbol | None = "SPY"
    risk_free_rate_pct: Decimal = Field(default=Decimal(0), ge=0, le=20)

    @field_validator("start", "out_of_sample_start", mode="before")
    @classmethod
    def _parse_start(cls, value: object) -> object:
        return None if value is None else parse_utc_bound(value, is_end=False)

    @field_validator("end", mode="before")
    @classmethod
    def _parse_end(cls, value: object) -> datetime:
        return parse_utc_bound(value, is_end=True)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.adjustment is Adjustment.RAW:
            raise ValueError("backtests need adjusted prices: use 'all' or 'split', not 'raw'")
        if self.start >= self.end:
            raise ValueError("start must be before end")
        if self.end > utc_now():
            raise ValueError("end must not be in the future")
        if self.end - self.start > MAX_SPAN:
            raise ValueError("backtests are limited to 15 years")
        if self.out_of_sample_start is not None and not (
            self.start < self.out_of_sample_start < self.end
        ):
            raise ValueError("out_of_sample_start must fall between start and end")
        return self


class TradeView(BaseModel):
    symbol: str
    entry_bar: datetime
    exit_bar: datetime
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    initial_stop: Decimal
    net_pnl: Decimal
    r_multiple: Decimal | None
    fees: Decimal
    bars_held: int
    exit_reason: str


class EquityCurvePoint(BaseModel):
    timestamp: datetime
    equity: Decimal
    market_value: Decimal


class OpenTradeView(BaseModel):
    symbol: str
    entry_bar: datetime
    quantity: int
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal


class BenchmarkResult(BaseModel):
    symbol: str
    total_return_pct: float
    max_drawdown_pct: float
    strategy_excess_return_pct: float
    note: str = "Buy-and-hold from the first to the last close in the period."


class BacktestReport(BaseModel):
    id: int | None = None
    created_at: datetime | None = None
    mode: Literal["BACKTEST"] = "BACKTEST"
    strategy: str
    strategy_version: str
    parameters_hash: str
    request: dict
    data_fingerprint: str
    bars_used: int
    symbols_without_data: list[str]
    metrics: PerformanceMetrics
    in_sample: PerformanceMetrics | None = None
    out_of_sample: PerformanceMetrics | None = None
    benchmark: BenchmarkResult | None = None
    signal_stats: dict
    estimated_spread_and_slippage_cost: Decimal
    trades: list[TradeView]
    open_trades: list[OpenTradeView]
    equity_curve: list[EquityCurvePoint]
    warnings: list[str]
    disclaimer: str = DISCLAIMER


class BacktestSummary(BaseModel):
    id: int
    created_at: datetime
    strategy: str
    strategy_version: str
    symbols: list[str]
    start: datetime
    end: datetime
    total_return_pct: float
    max_drawdown_pct: float
    trades: int
    data_fingerprint: str


class BacktestService:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, default_feed: DataFeed
    ) -> None:
        self._sessions = session_factory
        self._default_feed = default_feed

    async def run(self, request: BacktestRequest) -> BacktestReport:
        strategy = TrendFollowingStrategy(request.parameters)
        feed = request.feed or self._default_feed
        warmup_start = request.start - timedelta(days=math.ceil(strategy.required_bars * 1.5) + 10)

        async with self._sessions() as session:
            bars = {
                symbol: await _load(
                    session, symbol, feed, request.adjustment, warmup_start, request.end
                )
                for symbol in request.symbols
            }
            benchmark_bars = (
                await _load(
                    session, request.benchmark, feed, request.adjustment, request.start, request.end
                )
                if request.benchmark
                else []
            )
        missing = [s for s, b in bars.items() if not any(x.timestamp >= request.start for x in b)]
        tradable = {s: b for s, b in bars.items() if s not in missing}
        if not tradable:
            raise BacktestRequestError(
                f"no stored {request.adjustment.value}-adjusted {feed.value} daily bars for "
                f"{', '.join(request.symbols)} in the requested period; ingest them first"
            )

        engine = BacktestEngine(
            strategy=strategy,
            limits=request.risk_limits,
            costs=request.cost_model,
            starting_cash=request.starting_cash,
        )
        # CPU-bound: run off the event loop so the API stays responsive.
        result = await asyncio.to_thread(engine.run, tradable, start=request.start, end=request.end)
        report = _build_report(request, strategy, feed, result, bars, benchmark_bars, missing)

        async with self._sessions.begin() as session:
            record = BacktestRecord(
                strategy=strategy.name,
                strategy_version=strategy.version,
                parameters_hash=strategy.parameters_hash(),
                request=report.request,
                data_fingerprint=report.data_fingerprint,
                bars_used=report.bars_used,
                report=report.model_dump(mode="json", exclude={"id", "created_at"}),
            )
            session.add(record)
            await session.flush()
            report.id = record.id
        async with self._sessions() as session:
            report.created_at = (await session.get_one(BacktestRecord, report.id)).created_at
        logger.info(
            "backtest completed",
            extra={"backtest_id": report.id, "trades": report.metrics.trades},
        )
        return report

    async def get(self, backtest_id: int) -> BacktestReport:
        async with self._sessions() as session:
            record = await session.get(BacktestRecord, backtest_id)
        if record is None:
            raise BacktestNotFoundError(f"backtest {backtest_id} not found")
        return BacktestReport(**record.report, id=record.id, created_at=record.created_at)

    async def list(self, *, limit: int) -> list[BacktestSummary]:
        async with self._sessions() as session:
            records = (
                await session.scalars(
                    select(BacktestRecord).order_by(BacktestRecord.id.desc()).limit(limit)
                )
            ).all()
        return [
            BacktestSummary(
                id=r.id,
                created_at=r.created_at,
                strategy=r.strategy,
                strategy_version=r.strategy_version,
                symbols=r.request["symbols"],
                start=r.request["start"],
                end=r.request["end"],
                total_return_pct=r.report["metrics"]["total_return_pct"],
                max_drawdown_pct=r.report["metrics"]["max_drawdown_pct"],
                trades=r.report["metrics"]["trades"],
                data_fingerprint=r.data_fingerprint,
            )
            for r in records
        ]


async def _load(
    session: AsyncSession,
    symbol: str,
    feed: DataFeed,
    adjustment: Adjustment,
    start: datetime,
    end: datetime,
) -> list[Bar]:
    records = await get_bars_after(
        session,
        SeriesKey(symbol, "1Day", feed, adjustment),
        after=start - timedelta(microseconds=1),
        through=end,
    )
    return [Bar.model_validate(r, from_attributes=True) for r in records]


def fingerprint(series: dict[str, Sequence[Bar]]) -> str:
    digest = hashlib.sha256()
    for symbol in sorted(series):
        for bar in series[symbol]:
            digest.update(
                f"{symbol}|{bar.timestamp.isoformat()}|{bar.open}|{bar.high}|{bar.low}|"
                f"{bar.close}|{bar.volume}\n".encode()
            )
    return digest.hexdigest()


def _segment_metrics(
    start_equity: Decimal,
    curve: list[EquityPoint],
    trades: list[ClosedTrade],
    risk_free: float,
) -> PerformanceMetrics:
    notional = sum(
        (t.entry_price * t.quantity + t.exit_price * t.quantity for t in trades), Decimal(0)
    )
    return compute_metrics(
        start_equity=start_equity,
        curve=curve,
        trades=trades,
        total_fees=sum((t.fees for t in trades), Decimal(0)),
        traded_notional=notional,
        risk_free_rate_pct=risk_free,
    )


def _build_report(
    request: BacktestRequest,
    strategy: TrendFollowingStrategy,
    feed: DataFeed,
    run: BacktestRun,
    bars: dict[str, list[Bar]],
    benchmark_bars: list[Bar],
    missing: list[str],
) -> BacktestReport:
    risk_free = float(request.risk_free_rate_pct)
    metrics = compute_metrics(
        start_equity=run.starting_cash,
        curve=run.curve,
        trades=run.trades,
        total_fees=run.total_fees,
        traded_notional=run.traded_notional,
        risk_free_rate_pct=risk_free,
    )
    in_sample = out_of_sample = None
    if request.out_of_sample_start is not None and run.curve:
        split = request.out_of_sample_start
        before = [p for p in run.curve if p.timestamp < split]
        after = [p for p in run.curve if p.timestamp >= split]
        if before and after:
            in_sample = _segment_metrics(
                run.starting_cash,
                before,
                [t for t in run.trades if t.entry_bar < split],
                risk_free,
            )
            out_of_sample = _segment_metrics(
                before[-1].equity,
                after,
                [t for t in run.trades if t.entry_bar >= split],
                risk_free,
            )

    benchmark = None
    warnings = [
        "Survivorship bias: the symbols were chosen today, so companies that were "
        "delisted or failed during the period are missing. Results are likely optimistic.",
        "Costs are assumptions (spread, slippage, commission); real costs vary with "
        "liquidity, order size and market conditions.",
        "Fills use adjusted prices, which differ from the prices that actually traded "
        "before later splits/dividends.",
    ]
    if benchmark_bars and request.benchmark:
        closes = [EquityPoint(b.timestamp, b.close, b.close) for b in benchmark_bars]
        bench_return = float((closes[-1].equity / closes[0].equity - 1) * 100)
        bench_dd, _ = max_drawdown(closes[0].equity, closes)
        benchmark = BenchmarkResult(
            symbol=request.benchmark,
            total_return_pct=bench_return,
            max_drawdown_pct=bench_dd,
            strategy_excess_return_pct=metrics.total_return_pct - bench_return,
        )
    elif request.benchmark:
        warnings.append(
            f"Benchmark {request.benchmark} has no stored {request.adjustment.value}-adjusted "
            f"bars for this period; ingest it to compare against buy-and-hold."
        )
    if metrics.trades < MIN_TRADES_FOR_CONFIDENCE:
        warnings.append(
            f"Only {metrics.trades} closed trades: far too few to draw statistical "
            f"conclusions (at least {MIN_TRADES_FOR_CONFIDENCE} is a bare minimum)."
        )
    if metrics.cagr_pct is None:
        warnings.append("Period shorter than one year: returns are not annualized.")
    if missing:
        warnings.append(f"No data in the period for: {', '.join(missing)} (excluded).")
    first = run.first_signal_evaluation
    if first is None or first > request.start + timedelta(days=7):
        warnings.append(
            "Not enough history before start for the strategy's indicators; it could only "
            f"begin evaluating on {first.date() if first else 'no date'}."
        )
    if feed is DataFeed.IEX:
        warnings.append(
            "IEX feed: volume is IEX-only, so the liquidity limit (share of average volume) "
            "is much stricter than with consolidated volume and may shrink positions."
        )
    if request.out_of_sample_start is not None:
        warnings.append(
            "Out-of-sample split uses fixed parameters. It is only meaningful if the "
            "parameters were chosen WITHOUT looking at the out-of-sample period."
        )

    used = {**bars}
    if request.benchmark:
        used[f"benchmark:{request.benchmark}"] = benchmark_bars
    return BacktestReport(
        strategy=strategy.name,
        strategy_version=strategy.version,
        parameters_hash=strategy.parameters_hash(),
        request={**request.model_dump(mode="json"), "feed": feed.value},
        data_fingerprint=fingerprint(used),
        bars_used=sum(len(b) for b in used.values()),
        symbols_without_data=missing,
        metrics=metrics,
        in_sample=in_sample,
        out_of_sample=out_of_sample,
        benchmark=benchmark,
        signal_stats={
            "evaluations": dict(run.evaluations),
            "entries_rejected_by_rule": dict(run.rejections),
            "order_events": _count_events(run),
        },
        estimated_spread_and_slippage_cost=run.slippage_cost,
        trades=[
            TradeView(
                symbol=t.symbol,
                entry_bar=t.entry_bar,
                exit_bar=t.exit_bar,
                quantity=t.quantity,
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                initial_stop=t.initial_stop,
                net_pnl=t.net_pnl,
                r_multiple=t.r_multiple.quantize(Decimal("0.01")) if t.r_multiple else None,
                fees=t.fees,
                bars_held=t.bars_held,
                exit_reason=t.exit_reason,
            )
            for t in run.trades
        ],
        open_trades=[OpenTradeView(**t) for t in run.open_trades],
        equity_curve=[
            EquityCurvePoint(timestamp=p.timestamp, equity=p.equity, market_value=p.market_value)
            for p in run.curve
        ],
        warnings=warnings,
    )


def _count_events(run: BacktestRun) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in run.events:
        key = f"{event.purpose}:{event.outcome}"
        counts[key] = counts.get(key, 0) + 1
    return counts
