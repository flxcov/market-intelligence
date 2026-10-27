"""Pure parsers for SEC EDGAR JSON. They validate shape and never fill in gaps."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from packages.sec.errors import SecResponseError

ARCHIVES = "https://www.sec.gov/Archives/edgar/data"


@dataclass(frozen=True)
class TickerEntry:
    cik: int
    ticker: str
    title: str


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    filing_date: date
    report_date: date | None
    primary_document: str
    items: str
    url: str


@dataclass(frozen=True)
class CompanyProfile:
    cik: int
    name: str
    tickers: list[str]
    exchanges: list[str]
    sic: str | None
    sic_description: str | None
    entity_type: str | None
    fiscal_year_end: str | None
    filings: list[Filing]


@dataclass(frozen=True)
class XbrlFact:
    concept: str
    unit: str
    start: date | None
    end: date
    value: Decimal
    accession: str
    form: str
    filed: date
    fy: int | None
    fp: str | None

    @property
    def duration_days(self) -> int | None:
        return (self.end - self.start).days if self.start else None


def filing_index_url(cik: int, accession: str) -> str:
    return f"{ARCHIVES}/{cik}/{accession.replace('-', '')}/{accession}-index.htm"


def parse_ticker_map(payload: dict[str, Any]) -> dict[str, TickerEntry]:
    entries = {}
    try:
        for row in payload.values():
            ticker = str(row["ticker"]).upper()
            entries[ticker] = TickerEntry(int(row["cik_str"]), ticker, str(row["title"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise _bad("company_tickers.json", exc) from None
    return entries


def parse_submissions(payload: dict[str, Any]) -> CompanyProfile:
    try:
        cik = int(payload["cik"])
        recent = payload["filings"]["recent"]
        columns = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")
        lengths = {len(recent[c]) for c in columns}
        if len(lengths) != 1:
            raise ValueError("filing columns have different lengths")
        items = recent.get("items") or [""] * lengths.pop()
        filings = [
            Filing(
                accession=accn,
                form=form,
                filing_date=date.fromisoformat(filed),
                report_date=date.fromisoformat(report) if report else None,
                primary_document=doc,
                items=str(item or ""),
                url=f"{ARCHIVES}/{cik}/{accn.replace('-', '')}/{doc}"
                if doc
                else filing_index_url(cik, accn),
            )
            for accn, form, filed, report, doc, item in zip(
                *(recent[c] for c in columns), items, strict=True
            )
        ]
        return CompanyProfile(
            cik=cik,
            name=str(payload["name"]),
            tickers=[str(t) for t in payload.get("tickers") or []],
            exchanges=[str(e) for e in payload.get("exchanges") or [] if e],
            sic=payload.get("sic") or None,
            sic_description=payload.get("sicDescription") or None,
            entity_type=payload.get("entityType") or None,
            fiscal_year_end=payload.get("fiscalYearEnd") or None,
            filings=filings,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _bad("submissions", exc) from None


def parse_facts(payload: dict[str, Any], taxonomy: str, concept: str) -> list[XbrlFact]:
    """Every reported value of one concept (all units), as provided. Empty if absent."""
    try:
        node = (payload.get("facts") or {}).get(taxonomy, {}).get(concept)
        if node is None:
            return []
        facts = []
        for unit, rows in node["units"].items():
            for row in rows:
                facts.append(
                    XbrlFact(
                        concept=f"{taxonomy}:{concept}",
                        unit=unit,
                        start=date.fromisoformat(row["start"]) if row.get("start") else None,
                        end=date.fromisoformat(row["end"]),
                        value=Decimal(row["val"]),
                        accession=str(row["accn"]),
                        form=str(row["form"]),
                        filed=date.fromisoformat(row["filed"]),
                        fy=int(row["fy"]) if row.get("fy") is not None else None,
                        fp=row.get("fp"),
                    )
                )
        return facts
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise _bad(f"companyfacts {concept}", exc) from None


def _bad(what: str, exc: Exception) -> SecResponseError:
    return SecResponseError(
        f"SEC {what} has an unexpected shape ({type(exc).__name__}: {exc})", provider="sec_edgar"
    )
