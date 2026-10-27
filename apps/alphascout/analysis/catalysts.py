"""Catalysts with explicit date status. An upcoming event never guarantees a move.

Date statuses, strongest first:
  confirmed         stated in a primary-source filing (e.g. a definitive proxy's meeting date)
  company_guided    the company said when, without a firm date (not derived yet)
  estimated         derived by us from the company's own filing cadence - a window, not a date
  reported          mentioned in news, not confirmed by a primary source
  rumored           anonymous sourcing; never scored
"""

import itertools
import re
from datetime import date, datetime, timedelta
from statistics import median
from typing import Literal

from pydantic import BaseModel

from packages.sec.parsing import Filing

DateStatus = Literal["occurred", "confirmed", "company_guided", "estimated", "reported", "rumored"]
Relevance = Literal["high", "medium", "low"]

ITEM_EVENTS: dict[str, tuple[str, Relevance]] = {
    "1.01": ("material agreement", "high"),
    "1.03": ("bankruptcy or receivership", "high"),
    "2.01": ("acquisition or disposition completed", "high"),
    "2.02": ("results of operations", "high"),
    "2.03": ("new debt obligation", "medium"),
    "3.02": ("unregistered sale of equity (dilution)", "high"),
    "4.01": ("auditor change", "medium"),
    "4.02": ("non-reliance on financial statements", "high"),
    "5.02": ("officer or director change", "medium"),
    "5.07": ("shareholder vote results", "low"),
    "7.01": ("Regulation FD disclosure", "low"),
    "8.01": ("other material event", "medium"),
}
STATUS_WEIGHT = {
    "confirmed": 1.0,
    "company_guided": 0.7,
    "estimated": 0.4,
    "reported": 0.2,
    "rumored": 0.0,
}
RELEVANCE_POINTS = {"high": 20, "medium": 12, "low": 6}
PERIODIC = {"10-Q", "10-K"}

_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_MEETING = re.compile(
    rf"annual meeting of (?:stock|share)holders[^.]{{0,200}}?will be held[^.]{{0,80}}?"
    rf"(?:on\s+)?(?:\w+day,\s+)?(?P<month>{_MONTHS})\s+(?P<day>\d{{1,2}}),\s+(?P<year>\d{{4}})",
    re.I,
)


class Catalyst(BaseModel):
    event_type: str
    description: str
    date_status: DateStatus
    event_date: date | None = None
    window_start: date | None = None
    window_end: date | None = None
    relevance: Relevance
    source_url: str | None
    source_kind: Literal["sec_filing", "derived_from_filings", "news"]
    excerpt: str | None = None
    note: str | None = None


def past_events(filings: list[Filing], as_of: date, limit: int = 15) -> list[Catalyst]:
    out = []
    for f in filings:
        if not f.form.startswith("8-K") or f.filing_date > as_of:
            continue
        for item in [i.strip() for i in f.items.split(",") if i.strip()]:
            if item in ITEM_EVENTS:
                name, relevance = ITEM_EVENTS[item]
                out.append(
                    Catalyst(
                        event_type=name,
                        description=f"8-K item {item}: {name}",
                        date_status="occurred",
                        event_date=f.filing_date,
                        relevance=relevance,
                        source_url=f.url,
                        source_kind="sec_filing",
                    )
                )
    return out[:limit]


def meeting_from_proxy(text: str, filing: Filing, as_of: date) -> Catalyst | None:
    """Annual meeting date stated in a definitive proxy (DEF 14A), if still upcoming."""
    match = _MEETING.search(text)
    if not match:
        return None
    try:
        when = datetime.strptime(
            f"{match['month']} {match['day']} {match['year']}", "%B %d %Y"
        ).date()
    except ValueError:
        return None
    if when < as_of:
        return None
    return Catalyst(
        event_type="annual shareholder meeting",
        description="Annual meeting date stated in the definitive proxy statement",
        date_status="confirmed",
        event_date=when,
        relevance="low",
        source_url=filing.url,
        source_kind="sec_filing",
        excerpt=match.group(0)[:300],
    )


def estimated_next_results(filings: list[Filing], as_of: date) -> Catalyst | None:
    """A window for the next periodic report, from the company's own filing cadence."""
    dates = sorted(
        {f.filing_date for f in filings if f.form in PERIODIC and f.filing_date <= as_of}
    )
    if len(dates) < 3:
        return None
    recent = dates[-5:]
    gaps = [(b - a).days for a, b in itertools.pairwise(recent)]
    expected = dates[-1] + timedelta(days=round(median(gaps)))
    if expected < as_of - timedelta(days=10):
        return None  # cadence broken (late filer?): do not guess
    return Catalyst(
        event_type="periodic results (10-Q/10-K)",
        description="Next periodic report, estimated from the last filing intervals",
        date_status="estimated",
        window_start=expected - timedelta(days=10),
        window_end=expected + timedelta(days=10),
        relevance="high",
        source_url=None,
        source_kind="derived_from_filings",
        note=f"median gap {round(median(gaps))} days over the last {len(gaps)} intervals; "
        "an estimate, not a company-announced date",
    )


def credibility_points(
    upcoming: list[Catalyst], as_of: date, horizon_months: int
) -> tuple[int, str]:
    horizon_end = as_of + timedelta(days=round(horizon_months * 30.44))
    best, why = 0, "no upcoming catalyst with a credible date"
    for c in upcoming:
        when = c.event_date or c.window_start
        if when is None or when > horizon_end:
            continue
        points = round(RELEVANCE_POINTS[c.relevance] * STATUS_WEIGHT[c.date_status])
        if points > best:
            best, why = points, f"{c.date_status} {c.relevance}-relevance event: {c.event_type}"
    return best, why


_HEADLINE_DATE = re.compile(
    rf"\b(?:on|by|before)\s+(?P<month>{_MONTHS})\s+(?P<day>\d{{1,2}})(?:,\s+(?P<year>\d{{4}}))?",
    re.I,
)


def news_catalysts(articles, as_of: date) -> list[Catalyst]:
    """Future dates named in news headlines: 'reported' (or 'rumored' when sourcing is
    anonymous). Never confirmed by news alone; duplicates and promotion are skipped."""
    out = []
    for a in articles:
        if a.duplicate_of or a.verification == "promotional_downgraded":
            continue
        match = _HEADLINE_DATE.search(a.headline)
        if not match:
            continue
        year = int(match["year"]) if match["year"] else as_of.year
        try:
            when = datetime.strptime(f"{match['month']} {match['day']} {year}", "%B %d %Y").date()
        except ValueError:
            continue
        if when < as_of and not match["year"]:
            when = when.replace(year=year + 1)
        if when < as_of:
            continue
        rumored = any("anonymous" in f for f in a.flags)
        out.append(
            Catalyst(
                event_type=a.category or "news-mentioned event",
                description=a.headline[:200],
                date_status="rumored" if rumored else "reported",
                event_date=when,
                relevance="medium",
                source_url=a.canonical_url,
                source_kind="news",
                note="date from a news headline; not confirmed by a primary source",
            )
        )
    return out
