"""AlphaScout deterministic analysis on synthetic SEC data (hand-checked numbers)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from apps.alphascout.analysis.evidence import Verification
from apps.alphascout.analysis.fundamentals import FactSource, extract_fundamentals
from apps.alphascout.analysis.red_team import Severity, review
from apps.alphascout.analysis.report import assemble, decide
from apps.alphascout.analysis.screening import ScreenFilters, ScreenInput, screen
from apps.alphascout.analysis.valuation import (
    ScenarioAssumptions,
    ValuationInputs,
    default_assumptions,
    scenario,
    value,
)
from packages.sec.parsing import parse_submissions
from tests.fixtures import sec_payloads as sp

D = Decimal
AS_OF = datetime(2024, 6, 3, 14, tzinfo=UTC)


def fundamentals(as_of: date | None = None, **kw):
    as_of = as_of or AS_OF.date()
    return extract_fundamentals(FactSource(sp.company_facts(**kw), sp.CIK, "url", AS_OF, as_of))


INPUTS = ValuationInputs(
    revenue=D(100_000_000),
    cash=D(30_000_000),
    debt=D(10_000_000),
    shares=D(21_000_000),
    reference_price=D(10),
    horizon_months=24,
    historical_growth=D("0.25"),
    historical_dilution=D("0.03"),
)


# --- fundamentals -------------------------------------------------------------------


def test_fundamentals_are_cited_to_the_filing() -> None:
    f = fundamentals()
    revenue = f.evidence["revenue"]
    assert revenue.value == D(100_000_000)
    assert revenue.verification is Verification.PRIMARY
    assert (revenue.form, revenue.filed, revenue.accession) == (
        "10-K",
        date(2024, 2, 20),
        "0001234567-24-000003",
    )
    assert revenue.source_url == (
        "https://www.sec.gov/Archives/edgar/data/1234567/000123456724000003/"
        "0001234567-24-000003-index.htm"
    )
    assert f.evidence["revenue_growth"].verification is Verification.COMPUTED
    assert f.evidence["revenue_growth"].inputs == ["revenue", "revenue_prior"]


def test_no_look_ahead_and_restatements_known_at_the_time() -> None:
    f = fundamentals()
    assert f.value("revenue") == D(100_000_000)  # FY2024 (filed 2025) is invisible
    assert f.value("cash") == D(30_000_000)  # the June 10-Q (filed August) is invisible
    assert f.value("revenue_prior") == D(80_000_000)  # restated value, not 79M
    # As of early 2024 only the FY2022 10-K exists: its first-reported value is used.
    early = fundamentals(as_of=date(2023, 6, 1))
    assert early.value("revenue") == D(79_000_000)
    assert "revenue_prior" in early.missing
    later = fundamentals(as_of=date(2025, 3, 1))
    assert later.value("revenue") == D(125_000_000)


def test_derived_metrics_are_exact() -> None:
    f = fundamentals()
    assert f.value("revenue_growth") == D("0.2500")
    assert f.value("free_cash_flow") == D(8_000_000)
    assert f.value("dilution_rate") == D("0.0300")
    assert "runway_months" not in f.evidence  # cash-generative: no runway computed


def test_runway_for_a_cash_burner() -> None:
    f = fundamentals(operating_cash_flow=-40_000_000)
    assert f.value("runway_months") == D("9.0")  # 30M / (40M / 12)


def test_missing_debt_is_flagged_not_hidden() -> None:
    f = fundamentals(include_debt=False)
    assert "debt" in f.missing
    assert any("treated as 0" in n for n in f.notes)


# --- valuation ----------------------------------------------------------------------


def test_scenarios_hand_checked() -> None:
    v = value(INPUTS)
    bear, base, bull = v.scenarios
    assert v.market_cap == D("210000000.00")
    assert v.current_ev_to_sales == D("1.9000")  # (210 + 10 - 30) / 100
    # base: 100M x 1.25^2 = 156.25M; x 1.9 = 296.875M; +30 -10 = 316.875M;
    # shares 21M x 1.03^2 = 22,278,900 -> 14.2230...
    assert base.future_revenue == D("156250000.00")
    assert base.equity_value == D("316875000.00")
    assert base.diluted_shares == D(22_278_900)
    assert base.implied_price == D("14.22")
    assert (bear.assumptions.revenue_cagr, bull.assumptions.revenue_cagr) == (
        D("0.1000"),
        D("0.4000"),
    )
    # bull: 100M x 1.4^2 = 196M x 2.66 = 521.36M + 20M = 541.36M / 22,278,900 = 24.2992
    assert bull.implied_price == D("24.30") and bull.reaches_2x
    assert not base.reaches_2x and bear.implied_price < D(10)
    assert v.assumptions_source.startswith("default heuristics")
    assert len(v.sensitivity) == 9


def test_required_growth_for_2x_round_trips() -> None:
    v = value(INPUTS)
    base = default_assumptions(INPUTS)[1]
    needed = base.model_copy(update={"revenue_cagr": v.required_cagr_for_2x})
    assert scenario(INPUTS, needed).implied_price == pytest.approx(D(20), abs=D("0.01"))


def test_doubling_ev_does_not_double_the_price() -> None:
    flat = ScenarioAssumptions(
        name="x",
        revenue_cagr=D(0),
        ev_to_sales=D("3.8"),
        annual_dilution=D("0.10"),
        rationale="EV doubles from 190M to 380M",
    )
    result = scenario(INPUTS, flat)
    assert result.enterprise_value == D("380000000.00")
    assert result.implied_price < D(20)  # dilution eats the doubling


def test_implausible_bull_is_flagged_not_trimmed() -> None:
    wild = ScenarioAssumptions(
        name="bull",
        revenue_cagr=D("0.9"),
        ev_to_sales=D(25),
        annual_dilution=D(0),
        rationale="x",
    )
    flags = scenario(INPUTS, wild).plausibility_flags
    assert len(flags) == 3
    assert scenario(INPUTS, wild).assumptions.revenue_cagr == D("0.9")


def test_user_assumptions_must_be_complete() -> None:
    with pytest.raises(ValueError, match="exactly bear, base and bull"):
        value(INPUTS, default_assumptions(INPUTS)[:2])


def test_negative_equity_gives_no_price() -> None:
    poor = INPUTS.model_copy(update={"debt": D(900_000_000)})
    assert value(poor).scenarios[0].implied_price is None


# --- screening ----------------------------------------------------------------------


def screen_input(**overrides):
    fields = dict(
        ticker="SYNT",
        name=sp.NAME,
        exchanges=["Nasdaq"],
        price=D(10),
        price_bar_at=datetime(2024, 5, 31, 4, tzinfo=UTC),
        avg_dollar_volume=D(5_000_000),
        shares_outstanding=D(21_000_000),
        as_of=AS_OF,
    )
    fields.update(overrides)
    return ScreenInput(**fields)


def test_screen_passes_and_flags_microcaps() -> None:
    result = screen(screen_input(), ScreenFilters())
    assert result.passed and result.market_cap == D(210_000_000)
    assert any("microcap" in f for f in result.flags)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"price": None, "price_bar_at": None}, "no stored price"),
        ({"price_bar_at": datetime(2024, 5, 20, 4, tzinfo=UTC)}, "stale"),
        ({"price": D(4)}, "below"),
        ({"shares_outstanding": None}, "shares outstanding"),
        ({"avg_dollar_volume": D(1000)}, "too low"),
        ({"exchanges": []}, "major U.S. exchange"),
        ({"exchanges": ["OTC"]}, "major U.S. exchange"),
        ({"price": D(200)}, "market cap"),
    ],
)
def test_screen_failures_say_why(overrides, reason) -> None:
    result = screen(screen_input(**overrides), ScreenFilters())
    assert not result.passed and any(reason in r for r in result.reasons), result.reasons


# --- red team and verdict -----------------------------------------------------------


def report_for(f, valuation, filings):
    findings = review(f, valuation, filings, AS_OF.date(), ["Nasdaq"])
    return assemble(
        symbol="SYNT",
        company=sp.NAME,
        cik=sp.CIK,
        exchanges=["Nasdaq"],
        sector=None,
        as_of=AS_OF,
        generated_at=AS_OF,
        screen=screen(screen_input(), ScreenFilters()),
        fundamentals=f,
        extra_evidence=[],
        valuation=valuation,
        findings=findings,
        filings=filings,
    )


def test_clean_candidate_is_watchlist_only() -> None:
    report = report_for(fundamentals(), value(INPUTS), parse_submissions(sp.submissions()).filings)
    assert report.verdict == "watchlist"
    assert report.score_total == 25 + 20 + 0 + 15 + 10 + 10
    assert report.thesis_confidence == "low"
    assert report.upcoming_catalysts == [] and "never guarantees" in report.catalyst_note
    assert [(e.date_status, e.event_type) for e in report.recent_events] == [
        ("occurred", "results of operations"),  # 8-K item 2.02 (9.01 exhibits not an event)
    ]
    assert "News not checked" in report.news_note
    assert "2x" in report.thesis and "will double" not in report.thesis.lower()


def test_hard_rejection_cannot_be_overridden_by_score_or_bull_case() -> None:
    restatement = ("0001234567-24-000012", "8-K", "2024-05-20", "2024-05-20", "r.htm", "4.02")
    filings = parse_submissions(sp.submissions(extra_filings=[restatement])).filings
    report = report_for(fundamentals(), value(INPUTS), filings)
    assert report.valuation.scenarios[2].reaches_2x  # the bull case still "doubles"
    assert report.score_total == 80  # and the score is unchanged ...
    assert report.verdict == "rejected"  # ... but the verdict is a rejection
    assert any("4.02" in r for r in report.verdict_reasons)


@pytest.mark.parametrize(
    ("kwargs", "rule"),
    [
        ({"operating_cash_flow": -40_000_000}, "cash_runway"),
        ({"diluted_prior": 15_000_000}, "dilution"),  # 37% dilution
    ],
)
def test_hard_rules(kwargs, rule) -> None:
    f = fundamentals(**kwargs)
    findings = review(f, value(INPUTS), [], AS_OF.date(), ["Nasdaq"])
    assert any(x.rule == rule and x.severity is Severity.HARD for x in findings)
    assert decide(findings, f, value(INPUTS))[0] == "rejected"


def test_late_filing_and_unlisted_are_hard() -> None:
    late = ("0001234567-24-000013", "NT 10-Q", "2024-05-16", "", "nt.htm", "")
    filings = parse_submissions(sp.submissions(extra_filings=[late])).filings
    rules = {x.rule for x in review(fundamentals(), value(INPUTS), filings, AS_OF.date(), [])}
    assert {"late_filing", "listing"} <= rules


def test_implausible_bull_rejects_the_thesis() -> None:
    wild = default_assumptions(INPUTS)
    wild[2] = wild[2].model_copy(update={"ev_to_sales": D(30)})
    findings = review(fundamentals(), value(INPUTS, wild), [], AS_OF.date(), ["Nasdaq"])
    assert any(x.rule == "bull_case_plausible" for x in findings)


def test_insufficient_evidence_and_no_path() -> None:
    f = fundamentals()
    assert decide([], f, None)[0] == "insufficient_evidence"
    flat = [
        a.model_copy(update={"revenue_cagr": D(0), "ev_to_sales": D("1.9")})
        for a in default_assumptions(INPUTS)
    ]
    assert decide([], f, value(INPUTS, flat))[0] == "no_2x_path"


def test_financing_filings_are_warnings() -> None:
    shelf = ("0001234567-24-000014", "S-3", "2024-04-01", "", "s3.htm", "")
    filings = parse_submissions(sp.submissions(extra_filings=[shelf])).filings
    [finding] = [
        x
        for x in review(fundamentals(), value(INPUTS), filings, AS_OF.date(), ["Nasdaq"])
        if x.rule == "financing_filings"
    ]
    assert finding.severity is Severity.WARNING and finding.evidence[0].endswith("s3.htm")
