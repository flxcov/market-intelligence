"""SYNTHETIC SEC EDGAR payloads in the documented response shapes (ticker map,
submissions, XBRL company facts). The company and every number are invented.

Scenario "SYNT" (CIK 1234567), research as of 2024-06-03:
  FY2023 revenue 100,000,000 (10-K filed 2024-02-20; the same 10-K also reports a
  27,000,000 Q4 figure tagged fp=FY, which is not annual); FY2022 revenue 80,000,000 as
  restated in that 10-K (79,000,000 as first reported in the FY2022 10-K).
  FY2024 revenue 125,000,000 filed 2025-02-20 - AFTER as_of, must never be used.
  Cash 30,000,000 and debt 10,000,000 at 2024-03-31 (10-Q filed 2024-05-10); cash
  5,000,000 at 2024-06-30 filed 2024-08-09 - after as_of.
  Operating cash flow 12,000,000, capex 4,000,000, net income 5,000,000 (FY2023).
  Diluted shares 20,600,000 (FY2023) vs 20,000,000 (FY2022); 21,000,000 outstanding.
"""

from typing import Any

CIK = 1234567
NAME = "SYNTHETIC EXAMPLE CORP"
FY23 = ("2023-01-01", "2023-12-31")
FY22 = ("2022-01-01", "2022-12-31")
FY24 = ("2024-01-01", "2024-12-31")
K22 = ("0001234567-23-000002", "10-K", "2023-02-21")
K23 = ("0001234567-24-000003", "10-K", "2024-02-20")
Q1_24 = ("0001234567-24-000010", "10-Q", "2024-05-10")
Q2_24 = ("0001234567-24-000020", "10-Q", "2024-08-09")
K24 = ("0001234567-25-000004", "10-K", "2025-02-20")


def ticker_map(**extra: dict[str, Any]) -> dict[str, Any]:
    rows = {"0": {"cik_str": CIK, "ticker": "SYNT", "title": NAME}}
    rows.update(extra)
    return rows


def _row(period, filing, value, fy=None, fp="FY"):
    accn, form, filed = filing
    row = {
        "end": period[1],
        "val": value,
        "accn": accn,
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed": filed,
    }
    if period[0] is not None:
        row["start"] = period[0]
    return row


def _concept(unit: str, *rows) -> dict[str, Any]:
    return {"label": "synthetic", "description": "synthetic", "units": {unit: list(rows)}}


def company_facts(
    *,
    operating_cash_flow: int = 12_000_000,
    include_debt: bool = True,
    diluted_prior: int = 20_000_000,
) -> dict[str, Any]:
    q1 = (None, "2024-03-31")
    us_gaap = {
        "Revenues": _concept(
            "USD",
            _row(FY22, K22, 79_000_000, 2022),
            _row(FY22, K23, 80_000_000, 2023),
            # 10-Ks also carry the 3-month Q4 figure, tagged fp=FY like the annual one.
            _row(("2023-10-01", "2023-12-31"), K23, 27_000_000, 2023),
            _row(FY23, K23, 100_000_000, 2023),
            _row(("2024-01-01", "2024-03-31"), Q1_24, 30_000_000, 2024, "Q1"),
            _row(FY24, K24, 125_000_000, 2024),
        ),
        "NetIncomeLoss": _concept("USD", _row(FY23, K23, 5_000_000, 2023)),
        "NetCashProvidedByUsedInOperatingActivities": _concept(
            "USD", _row(FY23, K23, operating_cash_flow, 2023)
        ),
        "PaymentsToAcquirePropertyPlantAndEquipment": _concept(
            "USD", _row(FY23, K23, 4_000_000, 2023)
        ),
        "CashAndCashEquivalentsAtCarryingValue": _concept(
            "USD",
            _row(q1, Q1_24, 30_000_000, 2024, "Q1"),
            _row((None, "2024-06-30"), Q2_24, 5_000_000, 2024, "Q2"),
        ),
        "WeightedAverageNumberOfDilutedSharesOutstanding": _concept(
            "shares",
            _row(FY22, K23, diluted_prior, 2023),
            _row(FY23, K23, 20_600_000, 2023),
        ),
    }
    if include_debt:
        us_gaap["LongTermDebt"] = _concept("USD", _row(q1, Q1_24, 10_000_000, 2024, "Q1"))
    return {
        "cik": CIK,
        "entityName": NAME,
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": _concept(
                    "shares", _row((None, "2024-04-30"), Q1_24, 21_000_000, 2024, "Q1")
                )
            },
            "us-gaap": us_gaap,
        },
    }


def submissions(*, exchanges=("Nasdaq",), extra_filings=()) -> dict[str, Any]:
    filings = [
        ("0001234567-24-000011", "8-K", "2024-05-15", "2024-05-14", "synt-8k.htm", "2.02,9.01"),
        (*Q1_24, "2024-03-31", "synt-10q.htm", ""),
        (*K23, "2023-12-31", "synt-10k.htm", ""),
        *extra_filings,
    ]
    columns = ["accessionNumber", "form", "filingDate", "reportDate", "primaryDocument", "items"]
    recent = {c: [f[i] for f in filings] for i, c in enumerate(columns)}
    recent.update({"acceptanceDateTime": ["" for _ in filings], "size": [1 for _ in filings]})
    return {
        "cik": str(CIK),
        "entityType": "operating",
        "sic": "7372",
        "sicDescription": "Services-Prepackaged Software",
        "name": NAME,
        "tickers": ["SYNT"],
        "exchanges": list(exchanges),
        "fiscalYearEnd": "1231",
        "filings": {"recent": recent, "files": []},
    }
