"""Red-team review: deterministic rules that can reject a thesis.

A HARD finding rejects the candidate. Nothing downstream (score, bull case, AI text)
can override it: the coordinator computes the verdict from these findings first.
"""

from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from apps.alphascout.analysis.fundamentals import Fundamentals
from apps.alphascout.analysis.valuation import Valuation
from packages.sec.parsing import Filing

FINANCING_FORMS = {"S-1", "S-1/A", "S-3", "S-3/A", "S-3ASR", "424B3", "424B5", "F-1", "F-3"}
LATE_FILING_FORMS = {"NT 10-K", "NT 10-Q", "NT 20-F"}
LOOKBACK = timedelta(days=730)


class Severity(StrEnum):
    HARD = "hard"
    WARNING = "warning"


class Finding(BaseModel):
    rule: str
    severity: Severity
    detail: str
    evidence: list[str] = []  # evidence keys or filing URLs


def review(
    fundamentals: Fundamentals,
    valuation: Valuation | None,
    filings: list[Filing],
    as_of: date,
    exchanges: list[str],
    news: list | None = None,
) -> list[Finding]:
    out: list[Finding] = []
    v = fundamentals.value

    def add(rule, severity, detail, evidence=()):
        out.append(Finding(rule=rule, severity=severity, detail=detail, evidence=list(evidence)))

    if valuation is None:
        add(
            "valuation_possible",
            Severity.WARNING,
            "no valuation: revenue, price or share count is missing or not positive",
        )
    revenue = v("revenue")
    if revenue is not None and revenue <= 0:
        add(
            "revenue_positive",
            Severity.WARNING,
            "EV/Sales is unusable without positive revenue; another method is needed",
            ["revenue"],
        )
    runway = v("runway_months")
    if runway is not None and runway < 12:
        add(
            "cash_runway",
            Severity.HARD,
            f"cash runway {runway} months at last year's burn: financing likely",
            ["runway_months"],
        )
    dilution = v("dilution_rate")
    if dilution is not None and dilution > Decimal("0.20"):
        add(
            "dilution",
            Severity.HARD,
            f"diluted share count grew {dilution} in a year",
            ["dilution_rate"],
        )
    elif dilution is not None and dilution > Decimal("0.05"):
        add(
            "dilution",
            Severity.WARNING,
            f"diluted share count grew {dilution} in a year",
            ["dilution_rate"],
        )
    if not exchanges:
        add("listing", Severity.HARD, "not listed on an exchange in SEC data (OTC or delisted)")
    fcf = v("free_cash_flow")
    if fcf is not None and fcf < 0:
        add(
            "free_cash_flow", Severity.WARNING, f"negative free cash flow {fcf}", ["free_cash_flow"]
        )
    growth = v("revenue_growth")
    if growth is not None and growth < 0:
        add("revenue_trend", Severity.WARNING, f"revenue declined {growth}", ["revenue_growth"])
    if "debt" in fundamentals.missing:
        add("debt_reported", Severity.WARNING, "debt not reported in XBRL; assumed 0")

    recent = [f for f in filings if as_of - LOOKBACK <= f.filing_date <= as_of]
    for f in recent:
        if f.form in LATE_FILING_FORMS:
            add("late_filing", Severity.HARD, f"{f.form} filed {f.filing_date}", [f.url])
        if f.form.startswith("8-K") and "4.02" in f.items.split(","):
            add(
                "restatement",
                Severity.HARD,
                f"8-K item 4.02 (non-reliance on financial statements) filed {f.filing_date}",
                [f.url],
            )
        if f.form.startswith("8-K") and "4.01" in f.items.split(","):
            add(
                "auditor_change",
                Severity.WARNING,
                f"8-K item 4.01 (auditor change) filed {f.filing_date}",
                [f.url],
            )
    financing = [f for f in recent if f.form in FINANCING_FORMS]
    if financing:
        add(
            "financing_filings",
            Severity.WARNING,
            f"{len(financing)} registration/prospectus filing(s) in 24 months (possible shelf, "
            "ATM or offering)",
            [f.url for f in financing[:5]],
        )

    unique = [a for a in news or [] if a.duplicate_of is None]
    financing = [
        a for a in unique if a.category == "offering" and a.verification == "corroborated_by_filing"
    ]
    if financing:
        add(
            "financing_news",
            Severity.WARNING,
            f"{len(financing)} offering/dilution news item(s) corroborated by SEC filings",
            [a.corroborating_filing for a in financing[:5]],
        )
    promotional = [a for a in unique if a.verification == "promotional_downgraded"]
    if len(unique) >= 3 and len(promotional) * 2 >= len(unique):
        add(
            "promotion",
            Severity.WARNING,
            f"{len(promotional)} of {len(unique)} recent articles use promotional language",
        )

    if valuation is not None:
        bull = valuation.scenarios[2]
        if bull.plausibility_flags:
            add(
                "bull_case_plausible",
                Severity.HARD,
                "bull case is implausible: " + "; ".join(bull.plausibility_flags),
            )
    return out


NOT_CHECKED = [
    "going-concern language and auditor opinions (filing text is not analysed yet)",
    "insider transactions (Form 4 details are not interpreted)",
    "warrants, convertibles and other equity claims beyond the diluted share count",
    "customer concentration, litigation and regulatory risk",
    "short interest, float and borrow availability",
]
