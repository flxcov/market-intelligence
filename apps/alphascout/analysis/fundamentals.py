"""Point-in-time fundamentals from SEC XBRL company facts. Deterministic and cited.

Only facts FILED on or before `as_of` are used, so a report never sees the future.
Annual values are identified by their period (350-380 days), not by the filing's
fy/fp tags, which describe the filing rather than the period.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from apps.alphascout.analysis.evidence import Evidence, Verification
from packages.sec.parsing import XbrlFact, filing_index_url, parse_facts

REVENUE = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"]
NET_INCOME = ["NetIncomeLoss"]
OPERATING_CASH_FLOW = ["NetCashProvidedByUsedInOperatingActivities"]
CAPEX = ["PaymentsToAcquirePropertyPlantAndEquipment"]
CASH = ["CashAndCashEquivalentsAtCarryingValue"]
DEBT = ["LongTermDebt"]
DEBT_PARTS = ["LongTermDebtNoncurrent", "LongTermDebtCurrent"]
DILUTED_SHARES = ["WeightedAverageNumberOfDilutedSharesOutstanding"]
SHARES_OUTSTANDING = "EntityCommonStockSharesOutstanding"  # dei, cover page
MAX_INSTANT_AGE = timedelta(days=200)  # older balance-sheet values are reported as stale


@dataclass(frozen=True)
class Fundamentals:
    evidence: dict[str, Evidence]
    missing: list[str]
    notes: list[str]

    def value(self, key: str) -> Decimal | None:
        item = self.evidence.get(key)
        return item.value if item else None


class FactSource:
    def __init__(self, payload: dict, cik: int, url: str, retrieved_at: datetime, as_of: date):
        self.payload, self.cik, self.url = payload, cik, url
        self.retrieved_at, self.as_of = retrieved_at, as_of

    def facts(self, concepts: list[str], unit: str, taxonomy: str = "us-gaap") -> list[XbrlFact]:
        # All listed concepts, not just the first one found: companies switch tags over time
        # (Apple reported us-gaap:Revenues until FY2018, then RevenueFromContractWith...), so
        # the first concept can hold only stale periods. Callers pick the period first.
        return [
            f
            for concept in concepts
            for f in parse_facts(self.payload, taxonomy, concept)
            if f.unit == unit and f.filed <= self.as_of
        ]

    def annual(self, concepts: list[str], unit: str = "USD", years_back: int = 0):
        annual = [f for f in self.facts(concepts, unit) if 350 <= (f.duration_days or 0) <= 380]
        ends = sorted({f.end for f in annual}, reverse=True)
        if len(ends) <= years_back:
            return None
        if years_back:  # the year before must really be ~a year earlier
            target = ends[0] - timedelta(days=365 * years_back)
            ends = [e for e in ends if abs((e - target).days) <= 20]
            if not ends:
                return None
        return _preferred([f for f in annual if f.end == ends[0]], concepts, taxonomy="us-gaap")

    def instant(self, concepts: list[str], unit: str = "USD", taxonomy: str = "us-gaap"):
        instants = [f for f in self.facts(concepts, unit, taxonomy) if f.start is None]
        if not instants:
            return None
        latest = max(f.end for f in instants)
        return _preferred([f for f in instants if f.end == latest], concepts, taxonomy)

    def evidence(self, key: str, claim: str, fact: XbrlFact) -> Evidence:
        return Evidence(
            key=key,
            claim=claim,
            value=fact.value,
            unit=fact.unit,
            verification=Verification.PRIMARY,
            source_url=filing_index_url(self.cik, fact.accession),
            source_title=f"{fact.form} filed {fact.filed} (XBRL {fact.concept})",
            publisher="U.S. SEC EDGAR",
            concept=fact.concept,
            period_start=fact.start,
            period_end=fact.end,
            form=fact.form,
            accession=fact.accession,
            filed=fact.filed,
            retrieved_at=self.retrieved_at,
        )


def _preferred(facts: list[XbrlFact], concepts: list[str], taxonomy: str) -> XbrlFact:
    # Same period under several concepts: the earlier-listed concept wins.
    order = [f"{taxonomy}:{c}" for c in concepts]
    best = min(order.index(f.concept) for f in facts)
    return _latest_filed([f for f in facts if order.index(f.concept) == best])


def _latest_filed(facts: list[XbrlFact]) -> XbrlFact:
    # The most recent filing about a period reflects any restatement known by as_of.
    return max(facts, key=lambda f: (f.filed, f.accession))


def extract_fundamentals(source: FactSource) -> Fundamentals:
    ev: dict[str, Evidence] = {}
    missing: list[str] = []
    notes: list[str] = []

    def take(key, claim, fact):
        if fact is None:
            missing.append(key)
        else:
            ev[key] = source.evidence(key, claim, fact)

    take("revenue", "Revenue, latest fiscal year", source.annual(REVENUE))
    take("revenue_prior", "Revenue, prior fiscal year", source.annual(REVENUE, years_back=1))
    take("net_income", "Net income (loss), latest fiscal year", source.annual(NET_INCOME))
    take(
        "operating_cash_flow",
        "Operating cash flow, latest fiscal year",
        source.annual(OPERATING_CASH_FLOW),
    )
    take("capex", "Capital expenditures, latest fiscal year", source.annual(CAPEX))
    take("cash", "Cash and cash equivalents, latest balance sheet", source.instant(CASH))
    take(
        "shares_outstanding",
        "Common shares outstanding (cover page)",
        source.instant([SHARES_OUTSTANDING], unit="shares", taxonomy="dei"),
    )
    take(
        "diluted_shares",
        "Weighted average diluted shares, latest fiscal year",
        source.annual(DILUTED_SHARES, unit="shares"),
    )
    take(
        "diluted_shares_prior",
        "Weighted average diluted shares, prior fiscal year",
        source.annual(DILUTED_SHARES, unit="shares", years_back=1),
    )

    debt = source.instant(DEBT)
    if debt is not None:
        ev["debt"] = source.evidence("debt", "Long-term debt incl. current portion", debt)
    else:
        parts = [source.instant([c]) for c in DEBT_PARTS]
        found = [p for p in parts if p is not None]
        if found and len({p.end for p in found}) == 1:
            for part in found:
                key = f"debt_{part.concept.split(':')[1]}"
                ev[key] = source.evidence(key, f"Debt component {part.concept}", part)
            ev["debt"] = _computed(
                "debt",
                "Debt = sum of reported components",
                sum(p.value for p in found),
                [k for k in ev if k.startswith("debt_")],
                "USD",
            )
        else:
            missing.append("debt")
            notes.append(
                "No long-term debt concept is reported in XBRL; debt is treated as 0 in the "
                "scenarios. Check the balance sheet before relying on them."
            )

    for key in ("cash", "debt", "shares_outstanding"):
        item = ev.get(key)
        if item and item.period_end and source.as_of - item.period_end > MAX_INSTANT_AGE:
            notes.append(f"{key} is as of {item.period_end}, more than 200 days before as_of.")

    _derive(ev)
    return Fundamentals(evidence=ev, missing=missing, notes=notes)


def _computed(key, claim, value, inputs, unit=None) -> Evidence:
    return Evidence(
        key=key,
        claim=claim,
        value=value,
        unit=unit,
        verification=Verification.COMPUTED,
        inputs=inputs,
    )


def _derive(ev: dict[str, Evidence]) -> None:
    def v(key):
        item = ev.get(key)
        return item.value if item else None

    revenue, prior = v("revenue"), v("revenue_prior")
    if revenue is not None and prior:
        ev["revenue_growth"] = _computed(
            "revenue_growth",
            "Revenue growth, latest vs prior fiscal year (fraction)",
            (revenue / prior - 1).quantize(Decimal("0.0001")),
            ["revenue", "revenue_prior"],
        )
    ocf, capex = v("operating_cash_flow"), v("capex")
    if ocf is not None and capex is not None:
        ev["free_cash_flow"] = _computed(
            "free_cash_flow",
            "Free cash flow = operating cash flow - capex",
            ocf - capex,
            ["operating_cash_flow", "capex"],
            "USD",
        )
    cash = v("cash")
    if cash is not None and ocf is not None and ocf < 0:
        ev["runway_months"] = _computed(
            "runway_months",
            "Cash runway at last year's operating burn (months)",
            (cash / (-ocf / 12)).quantize(Decimal("0.1")),
            ["cash", "operating_cash_flow"],
        )
    shares, shares_prior = v("diluted_shares"), v("diluted_shares_prior")
    if shares is not None and shares_prior:
        ev["dilution_rate"] = _computed(
            "dilution_rate",
            "Diluted share count growth, latest vs prior year (fraction)",
            (shares / shares_prior - 1).quantize(Decimal("0.0001")),
            ["diluted_shares", "diluted_shares_prior"],
        )
