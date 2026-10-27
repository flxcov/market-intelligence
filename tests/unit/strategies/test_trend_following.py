from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from apps.marketpilot.strategies.base import Outcome, StrategyEvaluation
from apps.marketpilot.strategies.trend_following import (
    TrendFollowingParams,
    TrendFollowingStrategy,
)
from tests.strategy_data import as_of_after, breakout_series, linear_closes, make_bars

D = Decimal
STRATEGY = TrendFollowingStrategy()


def evaluate(bars, *, strategy=STRATEGY, as_of=None) -> StrategyEvaluation:
    return strategy.evaluate("TEST", "1Day", bars, as_of=as_of or as_of_after(bars))


def checks_by_rule(evaluation: StrategyEvaluation) -> dict[str, bool]:
    return {c.rule: c.passed for c in evaluation.checks}


# --- the signal case, checked against hand-derived values ----------------------


def test_breakout_in_uptrend_produces_signal_with_exact_values() -> None:
    bars = breakout_series()
    evaluation = evaluate(bars)

    assert evaluation.outcome is Outcome.SIGNAL
    assert evaluation.last_completed_bar == bars[-1].timestamp
    assert evaluation.bars_used == 200
    assert all(checks_by_rule(evaluation).values())

    atr = D("15.5") / 14
    stop = D("141.8") - 2 * atr
    proposal = evaluation.proposal
    assert proposal is not None
    assert proposal.side == "long"
    assert proposal.entry.order == "market_on_next_open"
    assert proposal.entry.reference_price == D("141.8")
    assert proposal.entry.expires_after_bars == 1
    assert proposal.atr == D("1.1071")
    assert proposal.exit.initial_stop == stop.quantize(D("0.0001")) == D("139.5857")
    assert proposal.entry.cancel_if_open_below == D("139.5857")
    assert proposal.entry.cancel_if_open_above == (D("141.8") + atr).quantize(D("0.0001"))
    assert proposal.risk_per_share == (2 * atr).quantize(D("0.0001")) == D("2.2143")
    assert proposal.exit.trailing_exit_lookback_bars == 10
    assert proposal.exit.time_stop_bars == 60
    scores = proposal.score_components
    assert scores["relative_volume"] == D("1.5")
    # (135.136 - 120.109) / 120.109 * 100
    assert scores["trend_spread_pct"] == (
        (D("135.136") - D("120.109")) / D("120.109") * 100
    ).quantize(D("0.0001"))
    # (141.8 - 140.3) / (15.5 / 14)
    assert scores["breakout_size_atr"] == (D("1.5") / atr).quantize(D("0.0001"))


def test_check_details_show_the_numbers() -> None:
    details = {c.rule: c.detail for c in evaluate(breakout_series()).checks}
    assert "SMA50=135.1360" in details["trend_alignment"]
    assert "SMA200=120.1090" in details["trend_alignment"]
    assert "prior 20-bar high=140.3" in details["breakout"]


# --- no-signal cases -----------------------------------------------------------


def test_no_breakout_means_no_signal_but_all_rules_are_reported() -> None:
    bars = make_bars(linear_closes(201))  # steady rise never closes above the prior high
    evaluation = evaluate(bars)

    assert evaluation.outcome is Outcome.NO_SIGNAL
    assert evaluation.proposal is None
    assert checks_by_rule(evaluation) == {
        "trend_alignment": True,
        "above_slow_ma": True,
        "breakout": False,
        "valid_stop": True,
    }


def test_downtrend_fails_trend_rules() -> None:
    bars = make_bars(linear_closes(201, start=D(200), step=D("-0.2")))
    checks = checks_by_rule(evaluate(bars))
    assert checks["trend_alignment"] is False
    assert checks["above_slow_ma"] is False


def test_invalid_stop_blocks_signal() -> None:
    # Low-priced series where 10 ATRs exceed the price, so the stop would be negative.
    closes = [*linear_closes(200, start=D(5), step=D("0.01")), D("8")]
    params = TrendFollowingParams(stop_atr_multiple=D(10))
    evaluation = evaluate(make_bars(closes), strategy=TrendFollowingStrategy(params))
    assert evaluation.outcome is Outcome.NO_SIGNAL
    assert checks_by_rule(evaluation)["valid_stop"] is False


# --- data-quality gate ---------------------------------------------------------


def test_insufficient_history() -> None:
    evaluation = evaluate(make_bars(linear_closes(150)))
    assert evaluation.outcome is Outcome.INSUFFICIENT_DATA
    assert evaluation.checks[0].detail == "150 of 200 bars"
    assert evaluation.proposal is None


def test_stale_data() -> None:
    bars = breakout_series()
    evaluation = evaluate(bars, as_of=as_of_after(bars) + timedelta(days=5))
    assert evaluation.outcome is Outcome.STALE_DATA


def test_data_gap_in_window() -> None:
    bars = breakout_series()
    gappy = bars[:100] + [
        b.model_copy(update={"timestamp": b.timestamp + timedelta(days=10)}) for b in bars[100:]
    ]
    assert evaluate(gappy).outcome is Outcome.DATA_GAP


def test_unsupported_timeframe_is_rejected() -> None:
    with pytest.raises(ValueError, match="supports"):
        STRATEGY.evaluate("TEST", "1Hour", breakout_series(), as_of=as_of_after(breakout_series()))


# --- look-ahead protection and reproducibility -------------------------------------


def test_future_bars_do_not_change_the_result() -> None:
    bars = breakout_series()
    as_of = as_of_after(bars)
    future_crash = make_bars([D(1), D(1)], first=bars[-1].timestamp + timedelta(days=2))
    assert evaluate(bars + future_crash, as_of=as_of) == evaluate(bars, as_of=as_of)


def test_bar_still_forming_at_as_of_is_ignored() -> None:
    bars = breakout_series()
    # The breakout bar's interval ends 1 day after its timestamp; just before that it
    # must not be used, so the strategy sees the previous (non-breakout) bar instead.
    as_of = bars[-1].timestamp + timedelta(hours=23)
    evaluation = evaluate(bars, as_of=as_of)
    assert evaluation.last_completed_bar == bars[-2].timestamp
    assert evaluation.outcome is Outcome.NO_SIGNAL


def test_historical_replay_uses_only_data_known_then() -> None:
    bars = breakout_series()
    past = as_of_after(bars[:-1])
    assert evaluate(bars, as_of=past) == evaluate(bars[:-1], as_of=past)


def test_input_order_does_not_matter() -> None:
    bars = breakout_series()
    assert evaluate(list(reversed(bars)), as_of=as_of_after(bars)) == evaluate(bars)


def test_same_inputs_give_identical_results() -> None:
    bars = breakout_series()
    assert evaluate(bars) == evaluate(bars)


# --- parameters ------------------------------------------------------------------


def test_parameters_hash_is_stable_and_sensitive() -> None:
    default_hash = TrendFollowingStrategy().parameters_hash()
    assert default_hash == TrendFollowingStrategy(TrendFollowingParams()).parameters_hash()
    assert (
        default_hash
        == TrendFollowingStrategy(
            TrendFollowingParams(stop_atr_multiple=D("2.00"))
        ).parameters_hash()
    )
    assert (
        default_hash
        != TrendFollowingStrategy(
            TrendFollowingParams(stop_atr_multiple=D("2.5"))
        ).parameters_hash()
    )
    assert len(default_hash) == 64


def test_fast_must_be_shorter_than_slow() -> None:
    with pytest.raises(ValidationError, match="fast_ma_period"):
        TrendFollowingParams(fast_ma_period=200, slow_ma_period=200)


def test_unknown_parameters_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TrendFollowingParams(fast_ma=10)  # type: ignore[call-arg]


def test_required_bars_follows_parameters() -> None:
    params = TrendFollowingParams(fast_ma_period=5, slow_ma_period=10, breakout_lookback=30)
    assert TrendFollowingStrategy(params).required_bars == 31


# --- regression pin ---------------------------------------------------------------


def test_version_pin() -> None:
    """If this fails you changed the rules or defaults: bump the strategy version
    and update this pin, so stored signals stay attributable to the logic that made them."""
    assert (TrendFollowingStrategy.name, TrendFollowingStrategy.version) == (
        "trend_following",
        "1.0.0",
    )
    assert TrendFollowingStrategy().parameters() == {
        "fast_ma_period": 50,
        "slow_ma_period": 200,
        "breakout_lookback": 20,
        "atr_period": 14,
        "stop_atr_multiple": "2",
        "max_entry_gap_atr": "1",
        "exit_lookback": 10,
        "max_holding_bars": 60,
        "volume_lookback": 20,
        "max_bar_age_days": 4,
        "max_gap_days": 6,
    }


# --- exit rule (shared by the backtester and forward PAPER trading) ------------------------


def test_exit_rule_uses_only_the_previous_lookback_bars() -> None:
    rising = make_bars([D(100) + i for i in range(11)])  # closes 100..110
    # Previous 10 bars (closes 100..109) have lowest low 99.5: a close of 110 is no exit.
    assert STRATEGY.exit_reason(rising, bars_held=3) is None
    dropped = [*rising[:-1], rising[-1].model_copy(update={"close": D("99.4"), "low": D("98.9")})]
    assert STRATEGY.exit_reason(dropped, bars_held=3) == "trailing_exit"


def test_exit_needs_full_lookback_window() -> None:
    short = make_bars([D(100), D(90)])
    assert STRATEGY.exit_reason(short, bars_held=1) is None


def test_time_stop() -> None:
    bars = make_bars([D(100) + i for i in range(11)])
    assert STRATEGY.exit_reason(bars, bars_held=59) is None
    assert STRATEGY.exit_reason(bars, bars_held=60) == "time_stop"
