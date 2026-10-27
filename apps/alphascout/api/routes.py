"""AlphaScout endpoints. Research support only: nothing here places orders."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from sqlalchemy import select

from apps.alphascout.services.research import (
    Evaluation,
    ResearchRequest,
    ResearchService,
    ScanRequest,
    ScanResult,
    StoredReport,
)
from apps.marketpilot.api.dependencies import require_session_factory
from apps.marketpilot.api.security import require_write_access
from packages.database.models import ResearchEvidenceRecord
from packages.market_data.models import Symbol
from packages.sec.errors import SecConfigurationError

router = APIRouter(tags=["alphascout research"])
write = [Depends(require_write_access)]
ReportId = Annotated[int, Path(ge=1)]


def _service(request: Request, *, fetches: bool = False) -> ResearchService:
    """`fetches=True` for endpoints that call SEC; reads of stored reports work without it."""
    sec = request.app.state.sec
    if sec is None and fetches:
        raise SecConfigurationError(
            request.app.state.sec_error or "SEC client not configured", provider="sec_edgar"
        )
    settings = request.app.state.settings
    return ResearchService(sec, require_session_factory(request), feed=settings.alpaca_data_feed)


@router.post("/discovery/scan", dependencies=write)
async def scan(request: Request, body: ScanRequest) -> ScanResult:
    """Screen a small universe (max 25 tickers) using SEC data and stored prices."""
    return await _service(request, fetches=True).scan(body)


@router.post("/research/{symbol}", dependencies=write)
async def research(
    request: Request,
    symbol: Annotated[Symbol, Path()],
    body: Annotated[ResearchRequest | None, Body()] = None,
) -> StoredReport:
    """Research one company from primary sources and save a new report version."""
    return await _service(request, fetches=True).research(symbol, body or ResearchRequest())


@router.get("/research/{symbol}")
async def latest(request: Request, symbol: Annotated[Symbol, Path()]) -> StoredReport:
    return await _service(request).latest(symbol)


@router.get("/reports")
async def list_reports(
    request: Request, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[dict]:
    return await _service(request).list_reports(limit=limit)


@router.get("/reports/{report_id}")
async def get_report(request: Request, report_id: ReportId) -> StoredReport:
    return await _service(request).get(report_id)


@router.get("/reports/{report_id}/evaluation")
async def evaluate(request: Request, report_id: ReportId) -> Evaluation:
    """Outcomes since the report (1/5/20/60 trading days; 6/12/24 months) from stored bars."""
    return await _service(request).evaluate(report_id)


@router.get("/evidence/{symbol}")
async def evidence(request: Request, symbol: Annotated[Symbol, Path()]) -> list[dict]:
    """Evidence behind the latest report for a symbol, with sources."""
    report = await _service(request).latest(symbol)
    async with require_session_factory(request)() as session:
        rows = (
            await session.scalars(
                select(ResearchEvidenceRecord)
                .where(ResearchEvidenceRecord.report_id == report.id)
                .order_by(ResearchEvidenceRecord.id)
            )
        ).all()
    return [
        {
            "key": r.key,
            "claim": r.claim,
            "value": r.value,
            "unit": r.unit,
            "verification": r.verification,
            "source_url": r.source_url,
            "concept": r.concept,
            "period_end": r.period_end,
            "accession": r.accession,
            "filed": r.filed,
            "retrieved_at": r.retrieved_at,
        }
        for r in rows
    ]


@router.post("/news/{symbol}/ingest", dependencies=write)
async def ingest_news(
    request: Request,
    symbol: Annotated[Symbol, Path()],
    days: Annotated[int, Query(ge=1, le=365)] = 90,
) -> dict:
    """Fetch recent articles from the news provider and store them (idempotent)."""
    articles, note = await _service(request).ingest_news(symbol, days)
    return {"symbol": symbol, "received": len(articles), "note": note}


@router.get("/news/{symbol}")
async def stored_news(
    request: Request,
    symbol: Annotated[Symbol, Path()],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    """Stored articles (third-party text; verification status lives in research reports)."""
    rows = await _service(request).stored_news(symbol, limit=limit)
    return [
        {
            "provider": r.provider,
            "provider_id": r.provider_id,
            "headline": r.headline,
            "source": r.source,
            "url": r.url,
            "published_at": r.published_at,
            "fingerprint": r.fingerprint,
            "retrieved_at": r.retrieved_at,
        }
        for r in rows
    ]


@router.get("/catalysts")
async def catalysts(
    request: Request, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[dict]:
    """Upcoming catalysts from each symbol's latest report, soonest first, with date status."""
    rows = await _service(request).upcoming_catalysts(limit=limit)
    return [
        {
            "symbol": r.symbol,
            "report_id": r.report_id,
            "event_type": r.event_type,
            "description": r.description,
            "date_status": r.date_status,
            "event_date": r.event_date,
            "window_start": r.window_start,
            "window_end": r.window_end,
            "relevance": r.relevance,
            "source_url": r.source_url,
            "source_kind": r.source_kind,
        }
        for r in rows
    ]
