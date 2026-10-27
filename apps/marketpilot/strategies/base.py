"""Strategy interface, result models and the shared data-quality gate.

Every strategy goes through `Strategy.evaluate`, which:
  1. keeps only bars whose interval has fully ended at `as_of` (no look-ahead,
     no partially formed bars), so a past `as_of` reproduces past results;
  2. refuses to evaluate on too little, stale, or gappy data;
  3. runs the strategy's own rules inside a fixed Decimal context.

Scores reported by strategies are descriptive components, NOT probabilities.
"""

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum
from itertools import pairwise
from typing import Any, ClassVar, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from packages.market_data.models import Bar, Timeframe

DECIMAL_PRECISION = 28
PRICE_QUANTUM = Decimal("0.0001")
RATIO_QUANTUM = Decimal("0.0001")


def quantize(value: Decimal, quantum: Decimal = PRICE_QUANTUM) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_EVEN)


class Outcome(StrEnum):
    SIGNAL = "signal"
    NO_SIGNAL = "no_signal"
    INSUFFICIENT_DATA = "insufficient_data"
    STALE_DATA = "stale_data"
    DATA_GAP = "data_gap"


class RuleCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: str
    passed: bool
    detail: str


class EntryPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    order: Literal["market_on_next_open"] = "market_on_next_open"
    reference_price: Decimal = Field(description="Close of the signal bar; planning only.")
    cancel_if_open_below: Decimal
    cancel_if_open_above: Decimal
    expires_after_bars: int


class ExitPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    initial_stop: Decimal
    trailing_exit_lookback_bars: int
    time_stop_bars: int
    rules: list[str]


class SignalProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    side: Literal["long"] = "long"
    entry: EntryPlan
    exit: ExitPlan
    atr: Decimal
    risk_per_share: Decimal = Field(description="reference_price - initial_stop")
    risk_pct_of_price: Decimal
    score_components: dict[str, Decimal | None] = Field(
        description="Descriptive measurements, not probabilities."
    )


class StrategyEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy: str
    version: str
    parameters_hash: str
    symbol: str
    timeframe: str
    as_of: AwareDatetime
    last_completed_bar: AwareDatetime | None
    bars_used: int
    outcome: Outcome
    checks: list[RuleCheck]
    proposal: SignalProposal | None = None


class Strategy(ABC):
    name: ClassVar[str]
    version: ClassVar[str]  # bump whenever the rules change
    supported_timeframes: ClassVar[frozenset[str]]

    def __init__(self, params: BaseModel) -> None:
        self.params = params

    @property
    @abstractmethod
    def required_bars(self) -> int:
        """Completed bars needed for one evaluation."""

    @property
    @abstractmethod
    def max_bar_age(self) -> timedelta:
        """How long after a bar ends it may still be used as the latest bar."""

    @property
    @abstractmethod
    def max_gap(self) -> timedelta:
        """Largest allowed spacing between consecutive bar timestamps."""

    @abstractmethod
    def _evaluate_rules(self, bars: Sequence[Bar]) -> tuple[list[RuleCheck], SignalProposal | None]:
        """Apply the rules to exactly `required_bars` completed bars, oldest first."""

    def parameters(self) -> dict[str, Any]:
        return self.params.model_dump(mode="json")

    def parameters_hash(self) -> str:
        """Stable fingerprint of (name, version, parameters) for reproducibility."""
        canonical = json.dumps(
            {"strategy": self.name, "version": self.version, "parameters": self.parameters()},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def evaluate(
        self, symbol: str, timeframe: str, bars: Sequence[Bar], *, as_of: datetime
    ) -> StrategyEvaluation:
        if timeframe not in self.supported_timeframes:
            raise ValueError(f"{self.name} supports {sorted(self.supported_timeframes)} only")
        duration = Timeframe.parse(timeframe).approx_duration
        completed = sorted(
            (b for b in bars if b.timestamp + duration <= as_of), key=lambda b: b.timestamp
        )
        window = completed[-self.required_bars :]
        last = window[-1].timestamp if window else None

        def result(outcome: Outcome, checks: list[RuleCheck], proposal=None) -> StrategyEvaluation:
            return StrategyEvaluation(
                strategy=self.name,
                version=self.version,
                parameters_hash=self.parameters_hash(),
                symbol=symbol,
                timeframe=timeframe,
                as_of=as_of,
                last_completed_bar=last,
                bars_used=len(window),
                outcome=outcome,
                checks=checks,
                proposal=proposal,
            )

        if len(window) < self.required_bars:
            return result(
                Outcome.INSUFFICIENT_DATA,
                [check("enough_history", False, f"{len(window)} of {self.required_bars} bars")],
            )
        age = as_of - (window[-1].timestamp + duration)
        if age > self.max_bar_age:
            return result(
                Outcome.STALE_DATA,
                [check("fresh_data", False, f"latest bar ended {_days(age)} days before as_of")],
            )
        widest = max(
            (b.timestamp - a.timestamp for a, b in pairwise(window)),
            default=timedelta(0),
        )
        if widest > self.max_gap:
            return result(
                Outcome.DATA_GAP,
                [check("no_gaps", False, f"bars up to {_days(widest)} days apart in window")],
            )

        with localcontext() as ctx:
            ctx.prec = DECIMAL_PRECISION
            ctx.rounding = ROUND_HALF_EVEN
            checks, proposal = self._evaluate_rules(window)
        outcome = Outcome.SIGNAL if proposal is not None else Outcome.NO_SIGNAL
        return result(outcome, checks, proposal)


def check(rule: str, passed: bool, detail: str) -> RuleCheck:
    """Shorthand for building a RuleCheck."""
    return RuleCheck(rule=rule, passed=passed, detail=detail)


def _days(delta: timedelta) -> str:
    return f"{delta.total_seconds() / 86400:.1f}"
