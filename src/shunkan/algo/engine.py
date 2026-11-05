"""Backtest engine: replay + strategy + fills + charges.

Event ordering inside one tick is fixed and matters:

    1. resting orders from EARLIER ticks are matched against this tick; if the
       governor is already tripped they are cancelled first
    2. resulting fills update the position, then `on_fill` is called
    3. the strategy sees the tick and may place or cancel orders
    4. equity is marked and the governor is told
    5. while tripped, every working order is cancelled and, with
       `flatten_on_trip`, the position is flattened; a flatten refused by a
       crossed or non-positive touch, or only partly filled, retries next tick

Step 3 after step 1 is what stops a strategy from acting on a fill it has not
been told about yet. Latency in `FillConfig` stops anything placed in step 3
from executing against this same tick.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from shunkan.algo.charges import (
    DEFAULT_RATES,
    ChargeBreakdown,
    Rates,
    Segment,
    charge_leg,
)
from shunkan.algo.fills import Fill, FillConfig, FillEngine, Order, OrderStatus, Side
from shunkan.algo.metrics import Result, RoundTrip
from shunkan.algo.replay import replay
from shunkan.algo.risk import Decision, RiskGovernor
from shunkan.stream.parser import Tick


@dataclass
class Position:
    """Net position at average cost. Handles the sign flip correctly, which
    is where hand-rolled P&L usually goes wrong."""

    quantity: int = 0
    avg_price: float = 0.0
    realized: float = 0.0
    opened_ns: int = 0

    def apply(self, price: float, signed_qty: int, ts_ns: int) -> RoundTrip | None:
        if signed_qty == 0:
            return None
        if self.quantity == 0:
            self.quantity = signed_qty
            self.avg_price = price
            self.opened_ns = ts_ns
            return None
        if (self.quantity > 0) == (signed_qty > 0):
            total = abs(self.quantity) + abs(signed_qty)
            self.avg_price = (
                self.avg_price * abs(self.quantity) + price * abs(signed_qty)
            ) / total
            self.quantity += signed_qty
            return None

        # Opposite direction: close against the existing position first.
        direction = 1 if self.quantity > 0 else -1
        closing = min(abs(self.quantity), abs(signed_qty))
        gross = (price - self.avg_price) * closing * direction
        self.realized += gross
        trip = RoundTrip(
            token=0,
            opened_ns=self.opened_ns,
            closed_ns=ts_ns,
            quantity=closing,
            entry=self.avg_price,
            exit=price,
            direction=direction,
            gross=gross,
            charges=0.0,  # attributed by the engine, which knows both legs
        )
        self.quantity += signed_qty
        if self.quantity == 0:
            self.avg_price = 0.0
        elif (self.quantity > 0) != (direction > 0):
            self.avg_price = price      # flipped through zero
            self.opened_ns = ts_ns
        return trip


@dataclass
class BacktestConfig:
    session: str
    root: Path | None = None
    tokens: list[int] | None = None
    start_ns: int | None = None
    end_ns: int | None = None
    segment: Segment = Segment.OPTION
    fills: FillConfig = field(default_factory=FillConfig)
    rates: Rates = DEFAULT_RATES
    flatten_at_end: bool = True
    event_clock: bool = True    # governor reads tick time, not the wall clock
    opening_qty: int = 0
    opening_price: float = 0.0


class Context:
    """Everything a strategy may see, and everything it may do."""

    def __init__(self, engine: "Engine") -> None:
        self._e = engine

    @property
    def tick(self) -> Tick:
        return self._e.tick

    @property
    def ts_ns(self) -> int:
        return self._e.ts_ns

    @property
    def position(self) -> int:
        return self._e.pos.quantity

    @property
    def avg_price(self) -> float:
        return self._e.pos.avg_price

    @property
    def realized(self) -> float:
        return self._e.pos.realized

    @property
    def working_orders(self) -> list[Order]:
        return self._e.fills.working

    def buy(self, quantity: int, limit: float | None = None) -> Order:
        return self._e.place(Side.BUY, quantity, limit)

    def sell(self, quantity: int, limit: float | None = None) -> Order:
        return self._e.place(Side.SELL, quantity, limit)

    def cancel(self, order_id: int) -> bool:
        return self._e.fills.cancel(order_id)

    def cancel_all(self) -> int:
        return sum(1 for o in self.working_orders if self._e.fills.cancel(o.id))


class Engine:
    """Drives a strategy over ticks, whatever their source.

    `run()` pulls from a recording; `step()` is public so a live feed can push
    into exactly the same machinery. Backtest and paper therefore share one
    implementation — a divergence between them is a data or latency
    difference, never a second code path that drifted.
    """

    def __init__(
        self,
        config: BacktestConfig,
        strategy,
        governor: RiskGovernor | None = None,
    ) -> None:
        if config.opening_qty and not (
            math.isfinite(config.opening_price) and config.opening_price > 0
        ):
            raise ValueError(
                f"opening position {config.opening_qty} needs a positive price, "
                f"got {config.opening_price}"
            )
        self.cfg = config
        self.strategy = strategy
        self.governor = governor
        if governor is not None and config.event_clock:
            governor.use_clock(lambda: self.ts_ns / 1e9)
        self.fills = FillEngine(config.fills)
        self.pos = Position(
            quantity=config.opening_qty,
            avg_price=config.opening_price if config.opening_qty else 0.0,
        )
        self.tick: Tick | None = None
        self.ts_ns: int = 0
        self.result = Result(
            strategy=getattr(strategy, "name", type(strategy).__name__),
            session=config.session,
        )
        self._ctx = Context(self)
        self.last_rejection: Decision | None = None
        self._charges = ChargeBreakdown()
        self._started = False

    def place(self, side: Side, quantity: int, limit: float | None) -> Order:
        """Every order in this package goes through here, and therefore
        through the governor when one is fitted. There is no other path."""
        assert self.tick is not None
        if self.governor is not None:
            decision = self.governor.check(
                token=self.tick.token,
                side=side,
                quantity=quantity,
                limit_price=limit,
                tick=self.tick,
                position=self.pos.quantity,
                ts_ns=self.ts_ns,
            )
            if not decision:
                self.result.orders_rejected += 1
                self.last_rejection = decision
                return Order(
                    id=-1, token=self.tick.token, side=side, quantity=quantity,
                    placed_ts_ns=self.ts_ns, active_from_ns=self.ts_ns,
                    limit_price=limit, status=OrderStatus.REJECTED,
                )
        o = self.fills.place(self.tick.token, side, quantity, self.ts_ns, limit)
        self.result.orders += 1
        return o

    def _on_fill(self, f: Fill) -> None:
        self.result.fills += 1
        self.result.fill_log.append(
            (f.ts_ns, f.token, f.side.value, f.price, f.quantity, f.is_maker)
        )
        if f.is_maker:
            self.result.maker_fills += 1
        else:
            self.result.taker_fills += 1
        leg = charge_leg(
            f.price, f.quantity, f.side is Side.BUY, self.cfg.segment, self.cfg.rates
        )
        self._charges = self._charges + leg
        trip = self.pos.apply(f.price, f.quantity * f.side.sign, f.ts_ns)
        if trip is not None:
            trip.token = f.token
            # Attribute both legs' charges to the trip that closed them, so
            # per-trade economics include the cost of getting in as well as out.
            entry_leg = charge_leg(
                trip.entry, trip.quantity, trip.direction > 0,
                self.cfg.segment, self.cfg.rates,
            )
            exit_leg = charge_leg(
                trip.exit, trip.quantity, trip.direction < 0,
                self.cfg.segment, self.cfg.rates,
            )
            trip.charges = entry_leg.total + exit_leg.total
            self.result.round_trips.append(trip)
            if self.governor is not None:
                self.governor.observe_round_trip(trip.net)
        hook = getattr(self.strategy, "on_fill", None)
        if hook is not None:
            hook(self._ctx, f)

    def step(self, tick: Tick, ts_ns: int) -> None:
        """Process one tick. Ordering is fixed and load-bearing:

        1. resting orders from EARLIER ticks match against this tick; if the
           governor is already tripped they are cancelled first
        2. fills update the position, then `on_fill` fires
        3. the strategy sees the tick and may act
        4. equity is marked and the governor is told
        5. while tripped: every working order is cancelled and, with
           `flatten_on_trip`, any position is flattened, every tick, until
           flat. A crossed or non-positive touch refuses the flatten for
           that tick.

        Step 3 after step 1 stops a strategy acting on a fill it has not been
        told about. `FillConfig.latency_ns` stops anything placed in step 3
        from executing against this same tick.
        """
        self.tick, self.ts_ns = tick, ts_ns
        if not self._started:
            start = getattr(self.strategy, "on_start", None)
            if start is not None:
                start(self._ctx)
            self._started = True
        if self.governor is not None:
            self.governor.observe_tick(tick, ts_ns)
            if self.governor.tripped:
                self._cancel_working()
        for f in self.fills.on_tick(tick, ts_ns):
            self._on_fill(f)
        self.strategy.on_tick(self._ctx)
        self.result.ticks += 1
        equity = self.pos.realized - self._charges.total + self._mtm()
        self.result.equity_curve.append((ts_ns, equity))
        if self.governor is not None:
            self.governor.observe_equity(equity)
            # A trip must close risk, not merely stop adding to it. Done here
            # rather than inside the governor because the governor deliberately
            # owns no execution surface — it decides, the engine acts. Checked
            # every tick: an order resting at the trip must never fill later,
            # and a flatten that found no book retries on the next tick.
            if self.governor.tripped:
                self._cancel_working()
                if (self.governor.limits.flatten_on_trip and self.pos.quantity
                        and self._flatten()):
                    self.result.flattened_on_trip = True
                    self.result.equity_curve.append(
                        (ts_ns, self.pos.realized - self._charges.total + self._mtm())
                    )

    def finalize(self) -> Result:
        """Close the run: flatten, snapshot results. Idempotent enough to be
        called from a `finally` when a live session is interrupted."""
        finish = getattr(self.strategy, "on_finish", None)
        if finish is not None:
            finish(self._ctx)
        if self.cfg.flatten_at_end and self.pos.quantity and self.tick is not None:
            self._flatten()
        self.result.realized_gross = self.pos.realized
        self.result.charges = self._charges
        self.result.open_position = self.pos.quantity
        self.result.open_avg_price = self.pos.avg_price
        self.result.mark_price = self.tick.mid if self.tick else 0.0
        if self.governor is not None:
            self.result.risk = self.governor.stats
        return self.result

    def run(self) -> Result:
        events = replay(
            self.cfg.session,
            root=self.cfg.root,
            tokens=self.cfg.tokens,
            start_ns=self.cfg.start_ns,
            end_ns=self.cfg.end_ns,
        )
        for ev in events:
            self.step(ev.tick, ev.ts_ns)
        return self.finalize()

    def _mtm(self) -> float:
        if not self.pos.quantity or self.tick is None:
            return 0.0
        return (self.tick.mid - self.pos.avg_price) * self.pos.quantity

    def _cancel_working(self) -> None:
        for o in self.fills.working:
            self.fills.cancel(o.id)

    def _flatten(self) -> int:
        """Close whatever is left at the final touch, paying to cross. Returns the qty filled.

        Carrying an open position into the result would let a losing strategy
        hide the loss as "unrealized". Marking it out at the price you would
        actually have received is the honest close. A crossed book, or a
        non-positive price on the side swept, is refused: it would book money
        that was never there. A one-sided book with the side we need is fine.
        """
        assert self.tick is not None
        qty = abs(self.pos.quantity)
        side = Side.SELL if self.pos.quantity > 0 else Side.BUY
        from shunkan.algo.book import is_crossed, sweep_buy, sweep_sell

        levels = self.tick.bids if side is Side.SELL else self.tick.asks
        if is_crossed(self.tick) or any(lv.price <= 0 for lv in levels):
            return 0
        s = sweep_sell(self.tick, qty) if side is Side.SELL else sweep_buy(self.tick, qty)
        if s.filled <= 0:
            return 0
        self._on_fill(
            Fill(
                order_id=-1, token=self.tick.token, ts_ns=self.ts_ns,
                side=side, price=s.vwap, quantity=s.filled, is_maker=False,
            )
        )
        return s.filled


def backtest(
    config: BacktestConfig, strategy, governor: RiskGovernor | None = None
) -> Result:
    """Run `strategy` over a recorded session and report net-of-cost results.

    Pass the same `governor` limits you intend to run live. A strategy that
    only works with the risk layer removed does not work.
    """
    return Engine(config, strategy, governor).run()
