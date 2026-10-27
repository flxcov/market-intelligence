"""Report coordinator: assembles a research report and decides its verdict.

Verdict order is fixed in code: any HARD red-team finding => "rejected", whatever
the score or the bull case says. Then missing core evidence => "insufficient_evidence".
Then no scenario reaching 2x => "no_2x_path". Only then "watchlist" - which is a
reason to keep researching, never a recommendation to buy.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from apps.alphascout.analysis.catalysts import Catalyst, credibility_points, past_events
from apps.alphascout.analysis.evidence import Evidence
from apps.alphascout.analysis.fundamentals import Fundamentals
from apps.alphascout.analysis.news import AnalysedArticle
from apps.alphascout.analysis.red_team import NOT_CHECKED, Finding, Severity
from apps.alphascout.analysis.screening import ScreenResult
from apps.alphascout.analysis.valuation import Valuation
from packages.sec.parsing import Filing

ENGINE_VERSION = "alphascout-0.1.0"
CORE = ("revenue", "cash", "shares_outstanding")
Verdict = Literal["rejected", "insufficient_evidence", "no_2x_path", "watchlist"]
DISCLAIMER = (
    "Research support only, not investment advice. Scenarios are hypothetical arithmetic on "
    "stated assumptions; scores are unvalidated heuristics, not probabilities. Nothing here "
    "says the stock will double."
)


class ScoreComponent(BaseModel):
    name: str
    points: int
    max_points: int
    reason: str


class ResearchReport(BaseModel):
    symbol: str
    company: str
    cik: int
    exchanges: list[str]
    sector: str | None
    as_of: datetime
    generated_at: datetime
    engine_version: str = ENGINE_VERSION
    screen: ScreenResult
    evidence: list[Evidence]
    missing_data: list[str]
    data_notes: list[str]
    recent_events: list[Catalyst]
    upcoming_catalysts: list[Catalyst]
    catalyst_note: str
    news: list[AnalysedArticle]
    news_note: str
    valuation: Valuation | None
    red_team: list[Finding]
    not_checked: list[str]
    score: list[ScoreComponent]
    score_total: int
    score_note: str
    verdict: Verdict
    verdict_reasons: list[str]
    thesis: str
    strongest_bear_case: str
    invalidation_conditions: list[str]
    data_confidence: Literal["low", "medium", "high"]
    thesis_confidence: Literal["low"]
    disclaimer: str = DISCLAIMER


def decide(
    findings: list[Finding], fundamentals: Fundamentals, valuation: Valuation | None
) -> tuple[Verdict, list[str]]:
    hard = [f for f in findings if f.severity is Severity.HARD]
    if hard:
        return "rejected", [f"{f.rule}: {f.detail}" for f in hard]
    missing = [k for k in CORE if k in fundamentals.missing]
    if missing or valuation is None:
        return "insufficient_evidence", [f"missing {k}" for k in missing] or ["no valuation"]
    if not valuation.scenarios[2].reaches_2x:
        return "no_2x_path", ["no scenario, including bull, reaches 2x the reference price"]
    return "watchlist", ["bull scenario reaches 2x under stated assumptions; verify further"]


def score(
    fundamentals: Fundamentals,
    valuation: Valuation | None,
    screen: ScreenResult,
    catalyst: tuple[int, str] = (0, "not assessed"),
) -> list[ScoreComponent]:
    v = fundamentals.value
    growth = v("revenue_growth")
    if growth is None:
        g = (0, "revenue growth unavailable")
    elif growth >= Decimal("0.20"):
        g = (25, f"revenue growth {growth} >= 0.20")
    elif growth >= Decimal("0.10"):
        g = (18, f"revenue growth {growth} >= 0.10")
    elif growth >= 0:
        g = (10, f"revenue growth {growth} >= 0")
    else:
        g = (0, f"revenue declined {growth}")
    if valuation is None:
        val = (0, "no valuation")
    elif valuation.scenarios[2].reaches_2x and (valuation.scenarios[1].return_pct or 0) > 0:
        val = (20, "bull reaches 2x and base is positive")
    elif valuation.scenarios[2].reaches_2x:
        val = (12, "only the bull scenario reaches 2x")
    else:
        val = (0, "no scenario reaches 2x")
    fcf, runway = v("free_cash_flow"), v("runway_months")
    if fcf is not None and fcf > 0:
        fin = (15, "positive free cash flow")
    elif runway is not None and runway >= 24:
        fin = (8, f"burning cash with {runway} months of runway")
    else:
        fin = (0, "negative or unknown cash generation")
    adv = screen.avg_dollar_volume
    if adv is not None and adv >= 5_000_000:
        liq = (10, "average dollar volume >= 5M")
    elif adv is not None and adv >= 1_000_000:
        liq = (5, "average dollar volume >= 1M")
    else:
        liq = (0, "thin or unknown liquidity")
    gaps = len(fundamentals.missing)
    ev = (max(10 - 2 * gaps, 0), f"{gaps} expected data item(s) missing")
    return [
        ScoreComponent(name="fundamentals_growth", points=g[0], max_points=25, reason=g[1]),
        ScoreComponent(name="valuation_path", points=val[0], max_points=20, reason=val[1]),
        ScoreComponent(
            name="catalyst_credibility", points=catalyst[0], max_points=20, reason=catalyst[1]
        ),
        ScoreComponent(name="financial_strength", points=fin[0], max_points=15, reason=fin[1]),
        ScoreComponent(name="liquidity", points=liq[0], max_points=10, reason=liq[1]),
        ScoreComponent(name="evidence_quality", points=ev[0], max_points=10, reason=ev[1]),
    ]


def narrative(valuation: Valuation | None, findings: list[Finding]) -> tuple[str, str, list[str]]:
    """Thesis, bear case and invalidation conditions, built only from computed numbers."""
    if valuation is None:
        return ("No thesis: the valuation could not be computed.", "Not assessed.", [])
    bear, base, bull = valuation.scenarios
    req = valuation.required_cagr_for_2x
    thesis = (
        f"Over {valuation.horizon_months} months, the base case implies "
        f"{base.implied_price} per share ({base.return_pct}%) and the bull case "
        f"{bull.implied_price} ({bull.return_pct}%) versus a reference price of "
        f"{valuation.reference_price}. "
    )
    thesis += (
        f"Reaching 2x at the base multiple and dilution needs revenue to compound at {req} "
        "per year."
        if req is not None
        else "Reaching 2x would need no revenue at the base multiple (check net cash)."
    )
    warnings = [f.detail for f in findings if f.severity is Severity.WARNING]
    bear_case = (
        f"Bear case: {bear.implied_price} per share ({bear.return_pct}%) with revenue CAGR "
        f"{bear.assumptions.revenue_cagr}, EV/Sales {bear.assumptions.ev_to_sales} and dilution "
        f"{bear.assumptions.annual_dilution}/yr."
        + (f" Risks found: {'; '.join(warnings)}." if warnings else "")
    )
    invalidation = [
        f"Revenue growth falls below the bear-case CAGR of {bear.assumptions.revenue_cagr}.",
        f"Share count grows faster than {bear.assumptions.annual_dilution} per year.",
        "Cash runway falls below 12 months, or a going-concern warning appears.",
        "A restatement (8-K item 4.02) or late filing (NT 10-K/10-Q) is filed.",
    ]
    return thesis, bear_case, invalidation


def assemble(
    *,
    symbol: str,
    company: str,
    cik: int,
    exchanges: list[str],
    sector: str | None,
    as_of: datetime,
    generated_at: datetime,
    screen: ScreenResult,
    fundamentals: Fundamentals,
    extra_evidence: list[Evidence],
    valuation: Valuation | None,
    findings: list[Finding],
    filings: list[Filing],
    upcoming: list[Catalyst] | None = None,
    news: list[AnalysedArticle] | None = None,
    news_note: str = "News not checked: no news provider configured.",
) -> ResearchReport:
    verdict, reasons = decide(findings, fundamentals, valuation)
    upcoming = upcoming or []
    horizon = valuation.horizon_months if valuation else 24
    components = score(
        fundamentals,
        valuation,
        screen,
        credibility_points(upcoming, as_of.date(), horizon),
    )
    thesis, bear_case, invalidation = narrative(valuation, findings)
    gaps = len(fundamentals.missing)
    return ResearchReport(
        symbol=symbol,
        company=company,
        cik=cik,
        exchanges=exchanges,
        sector=sector,
        as_of=as_of,
        generated_at=generated_at,
        screen=screen,
        evidence=[*fundamentals.evidence.values(), *extra_evidence],
        missing_data=fundamentals.missing,
        data_notes=fundamentals.notes,
        recent_events=past_events(filings, as_of.date()),
        upcoming_catalysts=sorted(
            upcoming, key=lambda c: c.event_date or c.window_start or as_of.date()
        ),
        catalyst_note=(
            "Upcoming events are labelled by date status: confirmed (primary filing), estimated "
            "(our window from filing cadence), reported/rumored (news only). An upcoming event "
            "never guarantees a price move. Past events are 8-K filings."
        ),
        news=news or [],
        news_note=news_note,
        valuation=valuation,
        red_team=findings,
        not_checked=NOT_CHECKED,
        score=components,
        score_total=sum(c.points for c in components),
        score_note="Heuristic points with unvalidated weights. Not a probability.",
        verdict=verdict,
        verdict_reasons=reasons,
        thesis=thesis,
        strongest_bear_case=bear_case,
        invalidation_conditions=invalidation,
        data_confidence="high" if gaps == 0 else "medium" if gaps <= 2 else "low",
        thesis_confidence="low",
    )
