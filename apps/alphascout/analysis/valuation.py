"""Bear/base/bull scenario valuation over an explicit horizon. Deterministic arithmetic.

Method: EV/Sales. Future revenue = revenue x (1 + CAGR)^years; EV = revenue x multiple;
equity = EV + cash - debt (today's balance sheet: a simplification, stated in the
report); per-share value = equity / (shares x (1 + dilution)^years).
A doubled enterprise value does NOT double per-share value: net debt and dilution
sit in between, which is why both are modelled.

Default assumptions are mechanical heuristics derived from the company's own history,
not forecasts. Users can pass their own. Implausible bull cases are flagged, never
adjusted to hit a target.
"""

from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel, Field, model_validator

CENT = Decimal("0.01")
BPS = Decimal("0.0001")
MAX_PLAUSIBLE_CAGR = Decimal("0.60")
MAX_PLAUSIBLE_MULTIPLE = Decimal("20")
MAX_MULTIPLE_EXPANSION = Decimal("2")  # bull multiple vs today's


class ScenarioAssumptions(BaseModel):
    name: str
    revenue_cagr: Decimal = Field(ge=Decimal("-0.9"), le=Decimal("5"))
    ev_to_sales: Decimal = Field(gt=0, le=Decimal("200"))
    annual_dilution: Decimal = Field(ge=0, le=Decimal("2"))
    rationale: str


class ValuationInputs(BaseModel):
    revenue: Decimal = Field(gt=0)
    cash: Decimal = Field(ge=0)
    debt: Decimal = Field(ge=0)
    shares: Decimal = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    horizon_months: int = Field(ge=12, le=36)
    historical_growth: Decimal | None = None
    historical_dilution: Decimal | None = None

    @property
    def years(self) -> Decimal:
        return Decimal(self.horizon_months) / 12

    @property
    def market_cap(self) -> Decimal:
        return self.reference_price * self.shares

    @property
    def enterprise_value(self) -> Decimal:
        return self.market_cap + self.debt - self.cash

    @property
    def current_ev_to_sales(self) -> Decimal:
        return self.enterprise_value / self.revenue


class ScenarioResult(BaseModel):
    assumptions: ScenarioAssumptions
    future_revenue: Decimal
    enterprise_value: Decimal
    equity_value: Decimal
    diluted_shares: Decimal
    implied_price: Decimal | None  # None when equity value is not positive
    return_pct: Decimal | None
    reaches_2x: bool
    plausibility_flags: list[str]


class Valuation(BaseModel):
    method: str = "EV/Sales scenario valuation"
    horizon_months: int
    reference_price: Decimal
    market_cap: Decimal
    current_ev_to_sales: Decimal
    scenarios: list[ScenarioResult]
    required_cagr_for_2x: Decimal | None  # at the base multiple and dilution
    sensitivity: list[dict]
    assumptions_source: str
    simplifications: list[str]

    @model_validator(mode="after")
    def _three(self):
        if [s.assumptions.name for s in self.scenarios] != ["bear", "base", "bull"]:
            raise ValueError("scenarios must be bear, base, bull")
        return self


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def default_assumptions(inputs: ValuationInputs) -> list[ScenarioAssumptions]:
    g = _clamp(inputs.historical_growth or Decimal(0), Decimal("-0.20"), Decimal("0.40"))
    m = _clamp(inputs.current_ev_to_sales, Decimal("0.5"), Decimal("15"))
    d = _clamp(inputs.historical_dilution or Decimal(0), Decimal(0), Decimal("0.25"))
    q = lambda x: x.quantize(BPS)  # noqa: E731
    return [
        ScenarioAssumptions(
            name="bear",
            revenue_cagr=q(max(g - Decimal("0.15"), Decimal("-0.30"))),
            ev_to_sales=q(m * Decimal("0.6")),
            annual_dilution=q(d + Decimal("0.05")),
            rationale="history-derived growth less 15pp, multiple -40%, dilution +5pp/yr",
        ),
        ScenarioAssumptions(
            name="base",
            revenue_cagr=q(g),
            ev_to_sales=q(m),
            annual_dilution=q(d),
            rationale="last year's growth (clamped to -20%..40%), today's EV/Sales, "
            "last year's dilution",
        ),
        ScenarioAssumptions(
            name="bull",
            revenue_cagr=q(min(g + Decimal("0.15"), Decimal("0.60"))),
            ev_to_sales=q(m * Decimal("1.4")),
            annual_dilution=q(d),
            rationale="history-derived growth plus 15pp, multiple +40%, same dilution",
        ),
    ]


def scenario(inputs: ValuationInputs, a: ScenarioAssumptions) -> ScenarioResult:
    years = inputs.years
    future_revenue = inputs.revenue * (1 + a.revenue_cagr) ** years
    ev = future_revenue * a.ev_to_sales
    equity = ev + inputs.cash - inputs.debt
    shares = inputs.shares * (1 + a.annual_dilution) ** years
    implied = (equity / shares).quantize(CENT, ROUND_HALF_EVEN) if equity > 0 else None
    flags = []
    if a.revenue_cagr > MAX_PLAUSIBLE_CAGR:
        flags.append(f"revenue CAGR {a.revenue_cagr} above {MAX_PLAUSIBLE_CAGR}")
    if a.ev_to_sales > MAX_PLAUSIBLE_MULTIPLE:
        flags.append(f"EV/Sales {a.ev_to_sales} above {MAX_PLAUSIBLE_MULTIPLE}")
    if a.ev_to_sales > inputs.current_ev_to_sales * MAX_MULTIPLE_EXPANSION:
        flags.append("multiple more than doubles from today's")
    return ScenarioResult(
        assumptions=a,
        future_revenue=future_revenue.quantize(CENT),
        enterprise_value=ev.quantize(CENT),
        equity_value=equity.quantize(CENT),
        diluted_shares=shares.quantize(Decimal(1)),
        implied_price=implied,
        return_pct=((implied / inputs.reference_price - 1) * 100).quantize(CENT)
        if implied is not None
        else None,
        reaches_2x=implied is not None and implied >= 2 * inputs.reference_price,
        plausibility_flags=flags,
    )


def required_cagr_for_2x(inputs: ValuationInputs, base: ScenarioAssumptions) -> Decimal | None:
    years = inputs.years
    shares = inputs.shares * (1 + base.annual_dilution) ** years
    ev_needed = 2 * inputs.reference_price * shares - inputs.cash + inputs.debt
    if ev_needed <= 0:
        return None
    revenue_needed = ev_needed / base.ev_to_sales
    return ((revenue_needed / inputs.revenue) ** (1 / years) - 1).quantize(BPS)


def value(
    inputs: ValuationInputs, assumptions: list[ScenarioAssumptions] | None = None
) -> Valuation:
    source = "user-supplied" if assumptions else "default heuristics from company history"
    assumptions = assumptions or default_assumptions(inputs)
    by_name = {a.name: a for a in assumptions}
    if sorted(by_name) != ["base", "bear", "bull"]:
        raise ValueError("assumptions must name exactly bear, base and bull")
    results = [scenario(inputs, by_name[n]) for n in ("bear", "base", "bull")]
    base = by_name["base"]
    sensitivity = []
    for dg in (Decimal("-0.05"), Decimal(0), Decimal("0.05")):
        for mult in (Decimal("0.8"), Decimal(1), Decimal("1.2")):
            varied = base.model_copy(
                update={
                    "revenue_cagr": max(base.revenue_cagr + dg, Decimal("-0.9")),
                    "ev_to_sales": (base.ev_to_sales * mult).quantize(BPS),
                }
            )
            result = scenario(inputs, varied)
            sensitivity.append(
                {
                    "revenue_cagr": str(varied.revenue_cagr),
                    "ev_to_sales": str(varied.ev_to_sales),
                    "implied_price": str(result.implied_price) if result.implied_price else None,
                }
            )
    return Valuation(
        horizon_months=inputs.horizon_months,
        reference_price=inputs.reference_price,
        market_cap=inputs.market_cap.quantize(CENT),
        current_ev_to_sales=inputs.current_ev_to_sales.quantize(BPS),
        scenarios=results,
        required_cagr_for_2x=required_cagr_for_2x(inputs, base),
        sensitivity=sensitivity,
        assumptions_source=source,
        simplifications=[
            "Cash and debt are today's balance sheet; future cash burn or generation is not "
            "modelled.",
            "Convertibles, warrants and options beyond the reported diluted share count are "
            "covered only through the dilution assumption.",
            "Scenario outputs are hypothetical arithmetic on stated assumptions, not "
            "forecasts or probabilities.",
        ],
    )
