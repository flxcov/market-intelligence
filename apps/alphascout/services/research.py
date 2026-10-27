"""AlphaScout vertical slice: scan a small universe, research one company from primary
sources, value three scenarios, red-team the thesis, and save a versioned report.

Every fetched SEC document is recorded (with its hash) before it is used, including
failed fetches. Prices come only from STORED raw daily bars, so a missing or stale
price is reported, never filled in.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.alphascout.analysis.catalysts import (
    Catalyst,
    estimated_next_results,
    meeting_from_proxy,
    news_catalysts,
)
from apps.alphascout.analysis.evidence import Evidence, Verification
from apps.alphascout.analysis.fundamentals import FactSource, extract_fundamentals
from apps.alphascout.analysis.news import analyse, canonical_url, fingerprint
from apps.alphascout.analysis.red_team import review
from apps.alphascout.analysis.report import ENGINE_VERSION, ResearchReport, assemble
from apps.alphascout.analysis.screening import ScreenFilters, ScreenInput, ScreenResult, screen
from apps.alphascout.analysis.valuation import ScenarioAssumptions, ValuationInputs, value
from packages.database.market_bars import SeriesKey, get_bars_after, get_latest_bars
from packages.database.models import (
    MarketBarRecord,
    NewsItemRecord,
    ResearchCatalystRecord,
    ResearchDocumentRecord,
    ResearchEvidenceRecord,
    ResearchReportRecord,
)
from packages.market_data.models import Adjustment, DataFeed, Symbol
from packages.market_data.news import NewsArticle, NewsRequest
from packages.market_data.provider import MarketDataProvider
from packages.sec.client import (
    COMPANY_FACTS_URL,
    SUBMISSIONS_URL,
    TICKERS_URL,
    SecDocument,
    SecEdgarClient,
)
from packages.sec.errors import SecConfigurationError, SecError, SecNotFoundError
from packages.sec.parsing import CompanyProfile, TickerEntry, parse_submissions, parse_ticker_map
from packages.shared.errors import AppError
from packages.shared.text import html_to_text
from packages.shared.timeutils import utc_now

logger = logging.getLogger(__name__)

DAILY = timedelta(days=1)
LIQUIDITY_BARS = 20
MAX_UNIVERSE = 25
OUTCOME_BARS = (1, 5, 20, 60)
OUTCOME_MONTHS = (6, 12, 24)


class ResearchNotFoundError(AppError):
    http_status = 404
    error_type = "not_found"


class ScanRequest(BaseModel):
    tickers: list[Symbol] = Field(min_length=1, max_length=MAX_UNIVERSE)
    filters: ScreenFilters = Field(default_factory=ScreenFilters)


class ResearchRequest(BaseModel):
    horizon_months: int = Field(default=24, ge=12, le=36)
    assumptions: list[ScenarioAssumptions] | None = Field(
        default=None, description="Exactly bear, base and bull. Default: history heuristics."
    )

    @field_validator("assumptions")
    @classmethod
    def _three(cls, value):
        if value is not None and sorted(a.name for a in value) != ["base", "bear", "bull"]:
            raise ValueError("assumptions must name exactly bear, base and bull")
        return value


class ScanItem(BaseModel):
    ticker: str
    result: ScreenResult | None
    error: str | None = None


class ScanResult(BaseModel):
    as_of: datetime
    filters: ScreenFilters
    items: list[ScanItem]
    note: str = (
        "Screens are experimental filters, not safety criteria. Passing a screen is not a "
        "thesis; run research on a candidate for evidence and scenarios."
    )


class StoredReport(BaseModel):
    id: int
    version: int
    created_at: datetime
    report: ResearchReport


class Outcome(BaseModel):
    horizon: str
    bar_at: datetime | None
    close: Decimal | None
    return_pct: Decimal | None
    status: str


class Evaluation(BaseModel):
    report_id: int
    symbol: str
    verdict: str
    reference_price: Decimal | None
    reference_bar_at: datetime | None
    outcomes: list[Outcome]
    max_favorable_pct: Decimal | None
    max_adverse_pct: Decimal | None
    note: str = (
        "Outcomes use stored raw daily closes after the report's reference bar only. Reports "
        "are never rewritten after the fact."
    )


class ResearchService:
    def __init__(
        self,
        sec: SecEdgarClient | None,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        feed: DataFeed,
        news: MarketDataProvider | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._news = news
        self._sec = sec
        self._sessions = session_factory
        self._feed = feed
        self._now = now
        self._tickers: dict[str, TickerEntry] | None = None
        self._documents: list[int] = []

    # --- discovery -------------------------------------------------------------------

    async def scan(self, request: ScanRequest) -> ScanResult:
        as_of = self._now()
        items = []
        for ticker in request.tickers:
            try:
                entry = await self._resolve(ticker)
                profile = parse_submissions(
                    (await self._fetch(SUBMISSIONS_URL.format(cik=entry.cik))).payload
                )
                facts = await self._fetch(COMPANY_FACTS_URL.format(cik=entry.cik))
                fundamentals = extract_fundamentals(
                    FactSource(
                        facts.payload, entry.cik, facts.url, facts.retrieved_at, as_of.date()
                    )
                )
                items.append(
                    ScanItem(
                        ticker=ticker,
                        result=await self._screen(
                            ticker, profile, fundamentals, as_of, request.filters
                        ),
                    )
                )
            except SecError as exc:
                items.append(ScanItem(ticker=ticker, result=None, error=exc.message))
        return ScanResult(as_of=as_of, filters=request.filters, items=items)

    # --- research --------------------------------------------------------------------

    async def research(self, symbol: str, request: ResearchRequest) -> StoredReport:
        as_of = self._now()
        self._documents = []
        entry = await self._resolve(symbol)
        subs = await self._fetch(SUBMISSIONS_URL.format(cik=entry.cik))
        profile = parse_submissions(subs.payload)
        facts = await self._fetch(COMPANY_FACTS_URL.format(cik=entry.cik))
        fundamentals = extract_fundamentals(
            FactSource(facts.payload, entry.cik, facts.url, facts.retrieved_at, as_of.date())
        )
        screened = await self._screen(symbol, profile, fundamentals, as_of, ScreenFilters())
        extra = self._market_evidence(screened)

        valuation = None
        v = fundamentals.value
        revenue, cash, shares = v("revenue"), v("cash"), v("shares_outstanding")
        if (
            screened.price is not None
            and revenue is not None
            and revenue > 0
            and cash is not None
            and shares
        ):
            inputs = ValuationInputs(
                revenue=revenue,
                cash=cash,
                debt=v("debt") or Decimal(0),
                shares=shares,
                reference_price=screened.price,
                horizon_months=request.horizon_months,
                historical_growth=v("revenue_growth"),
                historical_dilution=v("dilution_rate"),
            )
            valuation = value(inputs, request.assumptions)
            extra += [
                Evidence(
                    key=f"assumption.{a.assumptions.name}",
                    claim=f"{a.assumptions.name} scenario assumptions: {a.assumptions.rationale}",
                    value=None,
                    verification=Verification.ASSUMPTION,
                )
                for a in valuation.scenarios
            ]
        articles, news_note = await self._news_for(symbol, profile, as_of)
        upcoming = await self._upcoming(profile, as_of, articles)
        findings = review(
            fundamentals, valuation, profile.filings, as_of.date(), profile.exchanges, articles
        )
        report = assemble(
            symbol=symbol,
            company=profile.name,
            cik=profile.cik,
            exchanges=profile.exchanges,
            sector=profile.sic_description,
            as_of=as_of,
            generated_at=self._now(),
            screen=screened,
            fundamentals=fundamentals,
            extra_evidence=extra,
            valuation=valuation,
            findings=findings,
            filings=profile.filings,
            upcoming=upcoming,
            news=articles,
            news_note=news_note,
        )
        stored = await self._save(report)
        logger.info(
            "research report saved",
            extra={"symbol": symbol, "report_id": stored.id, "verdict": report.verdict},
        )
        return stored

    async def ingest_news(self, symbol: str, days: int = 90) -> tuple[list[NewsArticle], str]:
        """Fetch and store recent articles. Returns them with a short note."""
        if self._news is None:
            return [], "News not checked: no news provider is configured (Alpaca keys)."
        end = self._now()
        try:
            batch = await self._news.get_news(
                NewsRequest(symbol=symbol, start=end - timedelta(days=days), end=end)
            )
        except AppError as exc:
            async with self._sessions.begin() as session:
                session.add(
                    ResearchDocumentRecord(
                        url=f"alpaca news for {symbol}",
                        source="alpaca_news",
                        retrieved_at=end,
                        status="failed",
                        error_type=exc.error_type,
                        error_message=exc.message,
                    )
                )
            return [], f"News not checked: the news request failed ({exc.message})."
        async with self._sessions.begin() as session:
            for a in batch.articles:
                text = html_to_text(a.content)
                await session.execute(
                    insert(NewsItemRecord)
                    .values(
                        provider=a.provider,
                        provider_id=a.provider_id,
                        symbol=symbol,
                        headline=a.headline,
                        source=a.source,
                        author=a.author,
                        url=a.url,
                        canonical_url=canonical_url(a.url),
                        published_at=a.created_at,
                        updated_at=a.updated_at,
                        symbols=a.symbols,
                        summary=a.summary,
                        content_text=text,
                        fingerprint=fingerprint(a),
                        retrieved_at=batch.retrieved_at,
                    )
                    .on_conflict_do_nothing(constraint="uq_news_items_provider_id")
                )
        return batch.articles, " ".join(batch.notices)

    async def stored_news(self, symbol: str, *, limit: int) -> list[NewsItemRecord]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(NewsItemRecord)
                .where(NewsItemRecord.symbol == symbol)
                .order_by(NewsItemRecord.published_at.desc())
                .limit(limit)
            )
            return list(rows.all())

    async def upcoming_catalysts(self, *, limit: int) -> list[ResearchCatalystRecord]:
        """Upcoming catalysts from each symbol's LATEST report, soonest first."""
        today = self._now().date()
        async with self._sessions() as session:
            latest = (
                select(func.max(ResearchReportRecord.id))
                .group_by(ResearchReportRecord.symbol)
                .scalar_subquery()
            )
            when = func.coalesce(
                ResearchCatalystRecord.event_date, ResearchCatalystRecord.window_end
            )
            rows = await session.scalars(
                select(ResearchCatalystRecord)
                .where(
                    ResearchCatalystRecord.report_id.in_(latest),
                    ResearchCatalystRecord.date_status != "occurred",
                    when >= today,
                )
                .order_by(when)
                .limit(limit)
            )
            return list(rows.all())

    async def _news_for(self, symbol, profile, as_of):
        articles, note = await self.ingest_news(symbol)
        analysed = analyse(articles, profile.filings, as_of)
        if not analysed:
            return [], note
        unique = [a for a in analysed if a.duplicate_of is None]
        counts: dict[str, int] = {}
        for a in unique:
            counts[a.verification] = counts.get(a.verification, 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        return analysed, (
            f"{len(analysed)} article(s), {len(unique)} after de-duplication: {summary}. "
            "News is a lead, not proof; only filing-corroborated items are treated as facts."
        )

    async def _upcoming(self, profile, as_of, articles) -> list[Catalyst]:
        today = as_of.date()
        upcoming: list[Catalyst] = []
        estimate = estimated_next_results(profile.filings, today)
        if estimate is not None:
            upcoming.append(estimate)
        proxy = next(
            (
                f
                for f in profile.filings
                if f.form == "DEF 14A" and today - timedelta(days=400) <= f.filing_date <= today
            ),
            None,
        )
        if proxy is not None and self._sec is not None:
            try:
                document = await self._fetch_text(proxy.url)
            except SecError:
                pass  # recorded as a failed fetch; the meeting date stays unknown
            else:
                meeting = meeting_from_proxy(document.payload["text"], proxy, today)
                if meeting is not None:
                    upcoming.append(meeting)
        upcoming += news_catalysts(articles, today)
        return upcoming

    async def _fetch_text(self, url: str) -> SecDocument:
        try:
            document = await self._sec.get_text(url)
        except SecError as exc:
            await self._record_failure(url, exc)
            raise
        self._documents.append(await self._record(document))
        return document

    async def latest(self, symbol: str) -> StoredReport:
        async with self._sessions() as session:
            record = await session.scalar(
                select(ResearchReportRecord)
                .where(ResearchReportRecord.symbol == symbol)
                .order_by(ResearchReportRecord.version.desc())
                .limit(1)
            )
        if record is None:
            raise ResearchNotFoundError(f"no research report for {symbol}")
        return _stored(record)

    async def get(self, report_id: int) -> StoredReport:
        async with self._sessions() as session:
            record = await session.get(ResearchReportRecord, report_id)
        if record is None:
            raise ResearchNotFoundError(f"research report {report_id} not found")
        return _stored(record)

    async def list_reports(self, *, limit: int) -> list[dict]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(ResearchReportRecord)
                    .order_by(ResearchReportRecord.id.desc())
                    .limit(limit)
                )
            ).all()
        return [
            {
                "id": r.id,
                "symbol": r.symbol,
                "version": r.version,
                "verdict": r.verdict,
                "as_of": r.as_of,
                "reference_price": r.reference_price,
                "score_total": r.report.get("score_total"),
            }
            for r in rows
        ]

    async def evaluate(self, report_id: int) -> Evaluation:
        async with self._sessions() as session:
            record = await session.get(ResearchReportRecord, report_id)
            if record is None:
                raise ResearchNotFoundError(f"research report {report_id} not found")
            bars: list[MarketBarRecord] = []
            if record.reference_bar_at is not None:
                bars = await get_bars_after(
                    session,
                    SeriesKey(record.symbol, "1Day", self._feed, Adjustment.RAW),
                    after=record.reference_bar_at,
                    through=self._now() - DAILY,
                )
        ref = record.reference_price
        outcomes = []
        for n in OUTCOME_BARS:
            bar = bars[n - 1] if len(bars) >= n else None
            outcomes.append(_outcome(f"{n} trading day(s)", bar, ref))
        for months in OUTCOME_MONTHS:
            target = (record.reference_bar_at or record.as_of) + timedelta(
                days=round(months * 30.44)
            )
            bar = next((b for b in bars if b.timestamp >= target), None)
            outcomes.append(_outcome(f"{months} months", bar, ref))
        favorable = adverse = None
        if ref and bars:
            favorable = ((max(b.high for b in bars) / ref - 1) * 100).quantize(Decimal("0.01"))
            adverse = ((min(b.low for b in bars) / ref - 1) * 100).quantize(Decimal("0.01"))
        return Evaluation(
            report_id=record.id,
            symbol=record.symbol,
            verdict=record.verdict,
            reference_price=ref,
            reference_bar_at=record.reference_bar_at,
            outcomes=outcomes,
            max_favorable_pct=favorable,
            max_adverse_pct=adverse,
        )

    # --- helpers ---------------------------------------------------------------------

    async def _resolve(self, ticker: str) -> TickerEntry:
        if self._tickers is None:
            document = await self._fetch(TICKERS_URL)
            self._tickers = parse_ticker_map(document.payload)
        entry = self._tickers.get(ticker.replace(".", "-"))
        if entry is None:
            raise SecNotFoundError(f"{ticker} is not in SEC's ticker list", provider="sec_edgar")
        return entry

    async def _fetch(self, url: str) -> SecDocument:
        if self._sec is None:
            raise SecConfigurationError("SEC_USER_AGENT is not set", provider="sec_edgar")
        try:
            document = await self._sec.get_json(url)
        except SecError as exc:
            await self._record_failure(url, exc)
            raise
        self._documents.append(await self._record(document))
        return document

    async def _record(self, document: SecDocument) -> int:
        async with self._sessions.begin() as session:
            existing = await session.scalar(
                select(ResearchDocumentRecord.id).where(
                    ResearchDocumentRecord.url == document.url,
                    ResearchDocumentRecord.sha256 == document.sha256,
                    ResearchDocumentRecord.payload.is_not(None),
                )
            )
            record = ResearchDocumentRecord(
                url=document.url,
                source="sec_edgar",
                retrieved_at=document.retrieved_at,
                status="ok",
                sha256=document.sha256,
                # Same content already stored: keep the fetch, not a second copy.
                payload=None if existing else _jsonable(document.payload),
            )
            session.add(record)
            await session.flush()
            return record.id

    async def _record_failure(self, url: str, exc: SecError) -> None:
        async with self._sessions.begin() as session:
            session.add(
                ResearchDocumentRecord(
                    url=url,
                    source="sec_edgar",
                    retrieved_at=self._now(),
                    status="failed",
                    error_type=exc.error_type,
                    error_message=exc.message,
                )
            )

    async def _screen(self, ticker, profile: CompanyProfile, fundamentals, as_of, filters):
        async with self._sessions() as session:
            bars = await get_latest_bars(
                session,
                SeriesKey(ticker, "1Day", self._feed, Adjustment.RAW),
                end=as_of - DAILY,
                limit=LIQUIDITY_BARS,
            )
        last = bars[-1] if bars else None
        dollar_volume = (
            sum((b.close * b.volume for b in bars), Decimal(0)) / len(bars) if bars else None
        )
        return screen(
            ScreenInput(
                ticker=ticker,
                name=profile.name,
                exchanges=profile.exchanges,
                price=last.close if last else None,
                price_bar_at=last.timestamp if last else None,
                avg_dollar_volume=dollar_volume,
                shares_outstanding=fundamentals.value("shares_outstanding"),
                as_of=as_of,
            ),
            filters,
        )

    def _market_evidence(self, screened: ScreenResult) -> list[Evidence]:
        out = []
        if screened.price is not None:
            out.append(
                Evidence(
                    key="reference_price",
                    claim=f"Reference price: raw daily close ({self._feed} feed)",
                    value=screened.price,
                    unit="USD",
                    verification=Verification.MARKET_DATA,
                    source_title="stored market bars",
                    period_end=screened.price_bar_at.date() if screened.price_bar_at else None,
                )
            )
        if screened.market_cap is not None:
            out.append(
                Evidence(
                    key="market_cap",
                    claim="Market cap = reference price x shares outstanding",
                    value=screened.market_cap,
                    unit="USD",
                    verification=Verification.COMPUTED,
                    inputs=["reference_price", "shares_outstanding"],
                )
            )
        return out

    async def _save(self, report: ResearchReport) -> StoredReport:
        async with self._sessions.begin() as session:
            # Serialize versions per symbol.
            await session.execute(
                select(func.pg_advisory_xact_lock(72002, func.hashtext(report.symbol)))
            )
            version = (
                await session.scalar(
                    select(func.coalesce(func.max(ResearchReportRecord.version), 0)).where(
                        ResearchReportRecord.symbol == report.symbol
                    )
                )
            ) + 1
            record = ResearchReportRecord(
                symbol=report.symbol,
                cik=report.cik,
                version=version,
                as_of=report.as_of,
                verdict=report.verdict,
                reference_price=report.screen.price,
                reference_bar_at=report.screen.price_bar_at,
                engine_version=ENGINE_VERSION,
                report=report.model_dump(mode="json"),
                document_ids=list(self._documents),
                created_at=report.generated_at,
            )
            session.add(record)
            await session.flush()
            for e in report.evidence:
                session.add(
                    ResearchEvidenceRecord(
                        report_id=record.id,
                        key=e.key,
                        claim=e.claim,
                        value=e.value,
                        unit=e.unit,
                        verification=e.verification.value,
                        source_url=e.source_url,
                        concept=e.concept,
                        period_end=e.period_end,
                        accession=e.accession,
                        filed=e.filed,
                        retrieved_at=e.retrieved_at,
                    )
                )
            for c in [*report.upcoming_catalysts, *report.recent_events]:
                session.add(
                    ResearchCatalystRecord(
                        report_id=record.id,
                        symbol=report.symbol,
                        event_type=c.event_type[:64],
                        description=c.description,
                        date_status=c.date_status,
                        event_date=c.event_date,
                        window_start=c.window_start,
                        window_end=c.window_end,
                        relevance=c.relevance,
                        source_url=c.source_url,
                        source_kind=c.source_kind,
                        excerpt=c.excerpt,
                    )
                )
        return _stored(record)


def _stored(record: ResearchReportRecord) -> StoredReport:
    return StoredReport(
        id=record.id,
        version=record.version,
        created_at=record.created_at,
        report=ResearchReport.model_validate(record.report),
    )


def _outcome(horizon: str, bar: MarketBarRecord | None, ref: Decimal | None) -> Outcome:
    if bar is None or ref is None:
        return Outcome(
            horizon=horizon, bar_at=None, close=None, return_pct=None, status="not yet available"
        )
    return Outcome(
        horizon=horizon,
        bar_at=bar.timestamp,
        close=bar.close,
        return_pct=((bar.close / ref - 1) * 100).quantize(Decimal("0.01")),
        status="observed",
    )


def _jsonable(payload):
    """Decimals (from exact JSON parsing) become strings so JSONB keeps every digit."""
    if isinstance(payload, dict):
        return {k: _jsonable(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_jsonable(v) for v in payload]
    if isinstance(payload, Decimal):
        return str(payload)
    return payload
