"""Event-driven daily backtest engine. Pure: no database, no clock, no network.

Each session t is processed in this order:

  1. FILLS at bar t, for orders created at an EARLIER close. Per symbol, orders
     that execute at the open (market) go first, then stop/limit orders, which
     can only trigger later in the session. Entries pass the risk gate again at
     the actual fill price. A protective stop is placed on every entry and is
     live for the rest of that bar.
  2. MARK all positions at the close of t; record the equity point.
  3. DECISIONS at the close of t, for the NEXT session only:
       exits   close[t] below the lowest low of the previous N bars, or the
               position has been held `max_holding_bars` bars -> market sell;
       entries strategy evaluated on bars up to and including t (never later),
               sized at the worst admissible entry, risk-gated -> market buy
               valid for the next session only, with the signal's cancel bounds.

No step ever reads a bar after t, so results up to any date are unaffected by
data after it (verified by a truncation test). Bars before `start` are used only
as indicator warm-up; no trading happens before `start`.

Fills, costs, sizing, risk checks and accounting are the same functions PAPER
trading uses (`execution`, `risk`), so the two cannot drift apart silently.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from apps.marketpilot.backtesting.metrics import ClosedTrade, EquityPoint
from apps.marketpilot.execution.costs import CostModel
from apps.marketpilot.execution.fill_simulator import BarOutcome, simulate_on_bar
from apps.marketpilot.execution.models import OrderSide, OrderSpec, OrderType
from apps.marketpilot.execution.portfolio import AccountingError, Portfolio
from apps.marketpilot.risk.limits import AccountRiskState, EntryIntent, RiskLimits, check_entry
from apps.marketpilot.risk.position_sizing import size_entry
from apps.marketpilot.strategies.base import Outcome
from apps.marketpilot.strategies.indicators import mean
from apps.marketpilot.strategies.trend_following import TrendFollowingStrategy
from packages.market_data.models import Bar

DAILY = timedelta(days=1)


@dataclass
class _Order:
    id: int
    symbol: str
    spec: OrderSpec
    created_at: datetime  # close of the bar at which it was decided
    purpose: str  # entry | protective_stop | trailing_exit | time_stop
    day_only: bool
    protective_stop: Decimal | None = None
    live_from_bar: datetime | None = None  # protective stops: live from the entry bar


@dataclass
class _OpenTrade:
    symbol: str
    entry_bar: datetime
    entry_index: int
    quantity: int
    entry_price: Decimal
    initial_stop: Decimal
    entry_fee: Decimal


@dataclass
class OrderEvent:
    timestamp: datetime
    symbol: str
    purpose: str
    outcome: str  # filled | cancelled | expired | rejected
    detail: str


@dataclass
class BacktestRun:
    starting_cash: Decimal
    curve: list[EquityPoint]
    trades: list[ClosedTrade]
    open_trades: list[dict]
    events: list[OrderEvent]
    evaluations: Counter
    rejections: Counter
    total_fees: Decimal
    traded_notional: Decimal
    slippage_cost: Decimal
    first_signal_evaluation: datetime | None = None
    notes: list[str] = field(default_factory=list)


class BacktestEngine:
    def __init__(
        self,
        *,
        strategy: TrendFollowingStrategy,
        limits: RiskLimits,
        costs: CostModel,
        starting_cash: Decimal,
    ) -> None:
        self.strategy = strategy
        self.limits = limits
        self.costs = costs
        self.starting_cash = starting_cash

    def run(
        self, bars: Mapping[str, Sequence[Bar]], *, start: datetime, end: datetime
    ) -> BacktestRun:
        self.bars = {s: sorted(b, key=lambda x: x.timestamp) for s, b in bars.items()}
        self.index = {s: {b.timestamp: i for i, b in enumerate(v)} for s, v in self.bars.items()}
        self.portfolio = Portfolio()
        self.portfolio.deposit(self.starting_cash, start)
        self.orders: list[_Order] = []
        self.next_id = 1
        self.open_trades: dict[str, _OpenTrade] = {}
        self.marks: dict[str, Decimal] = {}
        self.equities: list[Decimal] = []
        self.run_result = BacktestRun(
            starting_cash=self.starting_cash,
            curve=[],
            trades=[],
            open_trades=[],
            events=[],
            evaluations=Counter(),
            rejections=Counter(),
            total_fees=Decimal(0),
            traded_notional=Decimal(0),
            slippage_cost=Decimal(0),
        )
        timeline = sorted(
            {b.timestamp for v in self.bars.values() for b in v if start <= b.timestamp <= end}
        )
        symbols = sorted(self.bars)
        for position, ts in enumerate(timeline):
            equity_before = self._equity()  # marks still hold the previous closes
            for symbol in symbols:
                if ts in self.index[symbol]:
                    self._fill_orders(symbol, ts, equity_before)
            for symbol in symbols:
                if ts in self.index[symbol]:
                    self.marks[symbol] = self.bars[symbol][self.index[symbol][ts]].close
            valuation = self.portfolio.valuation(self.marks)
            self.equities.append(valuation.equity)
            self.run_result.curve.append(EquityPoint(ts, valuation.equity, valuation.market_value))
            if position == len(timeline) - 1:
                break  # no session follows: decisions could never execute
            for symbol in symbols:
                if ts in self.index[symbol]:
                    self._decide(symbol, ts)

        result = self.run_result
        result.total_fees = self.portfolio.fees
        for symbol, trade in sorted(self.open_trades.items()):
            mark = self.marks[symbol]
            result.open_trades.append(
                {
                    "symbol": symbol,
                    "entry_bar": trade.entry_bar,
                    "quantity": trade.quantity,
                    "entry_price": trade.entry_price,
                    "mark_price": mark,
                    "unrealized_pnl": (mark - trade.entry_price) * trade.quantity,
                }
            )
        for order in self.orders:
            self._event(order, timeline[-1] if timeline else start, "expired", "backtest ended")
        return result

    # --- fills -------------------------------------------------------------------------

    def _fill_orders(self, symbol: str, ts: datetime, equity_before: Decimal) -> None:
        bar = self.bars[symbol][self.index[symbol][ts]]
        eligible = [o for o in self.orders if o.symbol == symbol and self._eligible(o, ts)]
        # Orders that execute at the open come before ones that trigger intraday.
        eligible.sort(key=lambda o: (o.spec.order_type is not OrderType.MARKET, o.id))
        queue = list(eligible)
        while queue:
            order = queue.pop(0)
            if order not in self.orders:
                continue
            outcome = simulate_on_bar(order.spec, bar, self.costs)
            if outcome.action == "cancel":
                self._close(order, ts, "cancelled", outcome.reason)
            elif outcome.action == "none":
                if order.day_only:
                    self._close(order, ts, "expired", "not filled in its session")
            elif order.spec.side is OrderSide.BUY:
                stop = self._fill_buy(order, bar, outcome, equity_before)
                if stop is not None:
                    queue.append(stop)
            else:
                self._fill_sell(order, bar, outcome)

    def _eligible(self, order: _Order, ts: datetime) -> bool:
        if order.live_from_bar is not None:
            return ts >= order.live_from_bar
        return ts > order.created_at

    def _fill_buy(
        self, order: _Order, bar: Bar, outcome: BarOutcome, equity_before: Decimal
    ) -> _Order | None:
        assert order.protective_stop is not None and outcome.fill_price is not None
        idx = self.index[order.symbol][bar.timestamp]
        state = self._risk_state(equity=equity_before, exclude=order)
        intent = EntryIntent(
            order.symbol,
            order.spec.quantity,
            outcome.base_price,
            order.protective_stop,
            bar.open,
            self._average_volume(order.symbol, idx - 1),
        )
        decision = check_entry(intent, state, self.limits, self.costs)
        if not decision.approved:
            self._close(
                order, bar.timestamp, "cancelled", "risk at fill: " + "; ".join(decision.failures)
            )
            return None
        fee = self.costs.commission(order.spec.quantity)
        try:
            self.portfolio.buy(
                order.symbol, order.spec.quantity, outcome.fill_price, fee, bar.timestamp
            )
        except AccountingError as exc:
            self._close(order, bar.timestamp, "cancelled", str(exc))
            return None
        self._record_costs(order, outcome)
        self._close(order, bar.timestamp, "filled", f"bought at {outcome.fill_price}")
        self.open_trades[order.symbol] = _OpenTrade(
            order.symbol,
            bar.timestamp,
            idx,
            order.spec.quantity,
            outcome.fill_price,
            order.protective_stop,
            fee,
        )
        stop = self._new_order(
            order.symbol,
            OrderSpec(
                side=OrderSide.SELL,
                order_type=OrderType.STOP,
                quantity=order.spec.quantity,
                stop_price=order.protective_stop,
            ),
            created_at=bar.timestamp,
            purpose="protective_stop",
            day_only=False,
        )
        stop.live_from_bar = bar.timestamp
        return stop

    def _fill_sell(self, order: _Order, bar: Bar, outcome: BarOutcome) -> None:
        assert outcome.fill_price is not None
        trade = self.open_trades.get(order.symbol)
        held = self.portfolio.positions.get(order.symbol)
        if trade is None or held is None or held.quantity < order.spec.quantity:
            self._close(order, bar.timestamp, "cancelled", "position already closed")
            return
        fee = self.costs.commission(order.spec.quantity)
        self.portfolio.sell(
            order.symbol, order.spec.quantity, outcome.fill_price, fee, bar.timestamp
        )
        self._record_costs(order, outcome)
        self._close(order, bar.timestamp, "filled", f"sold at {outcome.fill_price}")
        idx = self.index[order.symbol][bar.timestamp]
        gross = (outcome.fill_price - trade.entry_price) * trade.quantity
        self.run_result.trades.append(
            ClosedTrade(
                symbol=order.symbol,
                entry_bar=trade.entry_bar,
                exit_bar=bar.timestamp,
                quantity=trade.quantity,
                entry_price=trade.entry_price,
                exit_price=outcome.fill_price,
                initial_stop=trade.initial_stop,
                net_pnl=gross - trade.entry_fee - fee,
                fees=trade.entry_fee + fee,
                bars_held=idx - trade.entry_index,
                exit_reason=order.purpose,
            )
        )
        del self.open_trades[order.symbol]
        for other in [o for o in self.orders if o.symbol == order.symbol]:
            self._close(other, bar.timestamp, "cancelled", "position closed")

    # --- decisions at the close ---------------------------------------------------------

    def _decide(self, symbol: str, ts: datetime) -> None:
        idx = self.index[symbol][ts]
        history = self.bars[symbol][: idx + 1]  # nothing after t is ever visible
        pending = [o for o in self.orders if o.symbol == symbol]
        if symbol in self.open_trades:
            if any(o.purpose in ("trailing_exit", "time_stop") for o in pending):
                return
            self._maybe_exit(symbol, ts, idx, history)
            return
        if any(o.spec.side is OrderSide.BUY for o in pending):
            return
        evaluation = self.strategy.evaluate(symbol, "1Day", history, as_of=ts + DAILY)
        self.run_result.evaluations[evaluation.outcome.value] += 1
        if self.run_result.first_signal_evaluation is None and (
            evaluation.outcome in (Outcome.SIGNAL, Outcome.NO_SIGNAL)
        ):
            self.run_result.first_signal_evaluation = ts
        proposal = evaluation.proposal
        if evaluation.outcome is not Outcome.SIGNAL or proposal is None:
            return
        worst_entry = proposal.entry.cancel_if_open_above
        stop = proposal.exit.initial_stop
        state = self._risk_state(equity=self._equity())
        volume = self._average_volume(symbol, idx)
        sizing = size_entry(
            worst_entry_price=worst_entry,
            protective_stop=stop,
            average_volume=volume,
            state=state,
            limits=self.limits,
            costs=self.costs,
        )
        if sizing.quantity < 1:
            self.run_result.rejections[f"sizing:{sizing.limiting_factor}"] += 1
            return
        decision = check_entry(
            EntryIntent(symbol, sizing.quantity, worst_entry, stop, history[-1].close, volume),
            state,
            self.limits,
            self.costs,
        )
        if not decision.approved:
            for check in decision.checks:
                if not check.passed:
                    self.run_result.rejections[check.rule] += 1
            return
        self._new_order(
            symbol,
            OrderSpec(
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=sizing.quantity,
                cancel_if_open_below=proposal.entry.cancel_if_open_below,
                cancel_if_open_above=proposal.entry.cancel_if_open_above,
            ),
            created_at=ts,
            purpose="entry",
            day_only=True,
            protective_stop=stop,
        )

    def _maybe_exit(self, symbol: str, ts: datetime, idx: int, history: Sequence[Bar]) -> None:
        trade = self.open_trades[symbol]
        reason = self.strategy.exit_reason(history, idx - trade.entry_index)
        if reason is None:
            return
        self._new_order(
            symbol,
            OrderSpec(side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=trade.quantity),
            created_at=ts,
            purpose=reason,
            day_only=False,  # keep trying at each open until it fills
        )

    # --- helpers -------------------------------------------------------------------------

    def _risk_state(self, *, equity: Decimal, exclude: _Order | None = None) -> AccountRiskState:
        valuation = self.portfolio.valuation(self.marks)
        pending = [o for o in self.orders if o.spec.side is OrderSide.BUY and o is not exclude]
        previous = (
            self.equities[-2]
            if len(self.equities) >= 2
            else (self.starting_cash if self.equities else None)
        )
        return AccountRiskState(
            halted=False,
            equity=equity,
            cash=self.portfolio.cash,
            peak_equity=max([self.starting_cash, equity, *self.equities]),
            previous_equity=previous,
            position_quantities={
                s: p.quantity for s, p in self.portfolio.positions.items() if p.quantity
            },
            gross_position_value=valuation.market_value,
            pending_entry_symbols=frozenset(o.symbol for o in pending),
            pending_entry_cost=sum(
                (
                    self.costs.adverse_price(o.spec.cancel_if_open_above, OrderSide.BUY)
                    * o.spec.quantity
                    + self.costs.commission(o.spec.quantity)
                    for o in pending
                    if o.spec.cancel_if_open_above is not None
                ),
                Decimal(0),
            ),
        )

    def _average_volume(self, symbol: str, through_index: int) -> Decimal | None:
        lookback = self.limits.avg_volume_lookback
        window = self.bars[symbol][max(0, through_index - lookback + 1) : through_index + 1]
        return mean([b.volume for b in window]) if window else None

    def _equity(self) -> Decimal:
        return self.portfolio.valuation(self.marks).equity

    def _new_order(
        self,
        symbol: str,
        spec: OrderSpec,
        *,
        created_at: datetime,
        purpose: str,
        day_only: bool,
        protective_stop: Decimal | None = None,
    ) -> _Order:
        order = _Order(self.next_id, symbol, spec, created_at, purpose, day_only, protective_stop)
        self.next_id += 1
        self.orders.append(order)
        return order

    def _close(self, order: _Order, ts: datetime, outcome: str, detail: str) -> None:
        if order in self.orders:
            self.orders.remove(order)
            self._event(order, ts, outcome, detail)

    def _event(self, order: _Order, ts: datetime, outcome: str, detail: str) -> None:
        self.run_result.events.append(OrderEvent(ts, order.symbol, order.purpose, outcome, detail))

    def _record_costs(self, order: _Order, outcome: BarOutcome) -> None:
        assert outcome.fill_price is not None and outcome.base_price is not None
        quantity = order.spec.quantity
        self.run_result.traded_notional += outcome.fill_price * quantity
        self.run_result.slippage_cost += abs(outcome.fill_price - outcome.base_price) * quantity
