"""Order lifecycle and fill simulation.

This is the file that decides whether a backtest is worth anything. Every
optimistic assumption here shows up as profit that does not exist live, and
the failure is silent: the equity curve looks better, not broken.

The rules, and why each one is pessimistic on purpose:

1. NO SAME-TICK FILL. An order placed while processing tick N is not eligible
   until a tick whose timestamp is at least `latency_ns` later. Without this
   a strategy reacts to a print and trades at that same print -- the single
   most common way a scalping backtest invents an edge.

2. RESTING ORDERS NEED TRADE EVIDENCE. A limit buy at P fills only when the
   tape shows trading at or below P, or the offer comes down to P. Being
   "at the touch" is not a fill; queues exist.

3. QUEUE POSITION IS TRACKED. Join a level and you are behind everything
   already resting there. You fill only after that queue drains. The fraction
   of observed volume attributed to draining your side is `participation`
   (default 0.5) -- the single most consequential assumption in this module,
   because you cannot tell buy-initiated from sell-initiated trades from a
   quote feed. Lower it to be more conservative.

4. NEVER FILL MORE THAN TRADED. In one tick you cannot receive more than the
   volume that actually printed between this tick and the last.

5. THE VISIBLE BOOK IS THE LIMIT. A market order that exhausts five levels
   partially fills. It does not get invented liquidity at level six.

6. NO TRADING ON A BROKEN BOOK. Crossed, one-sided, or zero-priced books are
   skipped entirely. Feeds emit these around session boundaries -- notably the
   15:15-15:40 auction window -- and they look like free money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from shunkan.algo.book import is_tradeable, sweep_buy, sweep_sell
from shunkan.stream.parser import Tick


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderStatus(str, Enum):
    PENDING = "PENDING"      # placed, not yet live (latency)
    OPEN = "OPEN"            # live, unfilled
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}


@dataclass
class Fill:
    order_id: int
    token: int
    ts_ns: int
    side: Side
    price: float
    quantity: int
    is_maker: bool


@dataclass
class Order:
    id: int
    token: int
    side: Side
    quantity: int
    placed_ts_ns: int
    active_from_ns: int
    limit_price: float | None = None      # None => market order
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    notional: float = 0.0
    # Queue state, populated once the order is resting on the book.
    resting_price: float | None = None
    queue_ahead: int = 0
    fills: list[Fill] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled_qty

    @property
    def avg_price(self) -> float:
        return self.notional / self.filled_qty if self.filled_qty else 0.0

    @property
    def is_market(self) -> bool:
        return self.limit_price is None

    @property
    def done(self) -> bool:
        return self.status in TERMINAL

    def _record(self, ts_ns: int, price: float, qty: int, maker: bool) -> Fill:
        f = Fill(self.id, self.token, ts_ns, self.side, price, qty, maker)
        self.filled_qty += qty
        self.notional += price * qty
        self.fills.append(f)
        self.status = (
            OrderStatus.FILLED if self.remaining <= 0 else OrderStatus.PARTIAL
        )
        return f


@dataclass
class FillConfig:
    latency_ns: int = 50_000_000       # 50ms round trip to the exchange
    participation: float = 0.5         # share of printed volume that drains your queue
    allow_partial: bool = True
    max_levels: int = 5                # visible book depth


class FillEngine:
    """Simulates the exchange side: holds working orders, matches on ticks.

    Feed it every tick in order via `on_tick`. It returns the fills that
    happened *because of* that tick, which the engine applies to the book.
    """

    def __init__(self, config: FillConfig | None = None) -> None:
        self.cfg = config or FillConfig()
        self.orders: dict[int, Order] = {}
        self._next_id = 1
        self._prev_volume: dict[int, int] = {}

    # ---- order entry ----------------------------------------------------

    def place(
        self,
        token: int,
        side: Side,
        quantity: int,
        ts_ns: int,
        limit_price: float | None = None,
    ) -> Order:
        oid = self._next_id
        self._next_id += 1
        o = Order(
            id=oid,
            token=token,
            side=side,
            quantity=quantity,
            placed_ts_ns=ts_ns,
            active_from_ns=ts_ns + self.cfg.latency_ns,
            limit_price=limit_price,
        )
        if quantity <= 0:
            o.status = OrderStatus.REJECTED
        self.orders[oid] = o
        return o

    def cancel(self, order_id: int) -> bool:
        """Cancels are not instant either -- an order can fill in the gap.

        Modelled as immediate here, which is *optimistic*; a strategy that
        depends on cancel speed should be treated with suspicion until tested
        live. Flagged rather than hidden.
        """
        o = self.orders.get(order_id)
        if o is None or o.done:
            return False
        o.status = OrderStatus.CANCELLED
        return True

    @property
    def working(self) -> list[Order]:
        return [o for o in self.orders.values() if not o.done]

    # ---- matching -------------------------------------------------------

    def on_tick(self, tick: Tick, ts_ns: int) -> list[Fill]:
        vol_delta = self._volume_delta(tick)
        if not is_tradeable(tick):
            # Book is broken/one-sided. Record volume so the next usable tick
            # does not attribute this interval's trading to a queue drain.
            return []
        out: list[Fill] = []
        for o in list(self.orders.values()):
            if o.done or o.token != tick.token:
                continue
            if ts_ns < o.active_from_ns:
                continue
            if o.status is OrderStatus.PENDING:
                o.status = OrderStatus.OPEN
            out.extend(self._match(o, tick, ts_ns, vol_delta))
        return out

    def _volume_delta(self, tick: Tick) -> int:
        prev = self._prev_volume.get(tick.token)
        self._prev_volume[tick.token] = tick.volume
        if prev is None or tick.volume < prev:
            return 0  # first sight, or a feed reset — claim nothing traded
        return tick.volume - prev

    def _match(self, o: Order, tick: Tick, ts_ns: int, vol_delta: int) -> list[Fill]:
        if o.is_market:
            return self._take(o, tick, ts_ns, None)
        assert o.limit_price is not None
        lp = o.limit_price
        # Marketable on arrival: the far touch is already at or through our
        # limit, so we cross and pay the spread rather than rest.
        if (o.side is Side.BUY and tick.ask <= lp) or (
            o.side is Side.SELL and tick.bid >= lp
        ):
            return self._take(o, tick, ts_ns, lp)
        return self._rest(o, tick, ts_ns, vol_delta)

    def _take(
        self, o: Order, tick: Tick, ts_ns: int, cap: float | None
    ) -> list[Fill]:
        """Cross the spread, walking the visible book."""
        sweep = (
            sweep_buy(tick, o.remaining)
            if o.side is Side.BUY
            else sweep_sell(tick, o.remaining)
        )
        if sweep.filled <= 0:
            return []
        price = sweep.vwap
        if cap is not None:
            # A limit order never fills worse than its limit. Walking deep
            # enough to breach it means only the part inside the limit fills.
            if o.side is Side.BUY and price > cap:
                return self._take_capped(o, tick, ts_ns, cap)
            if o.side is Side.SELL and price < cap:
                return self._take_capped(o, tick, ts_ns, cap)
        qty = sweep.filled
        if not self.cfg.allow_partial and sweep.exhausted:
            return []
        return [o._record(ts_ns, price, qty, maker=False)]

    def _take_capped(self, o: Order, tick: Tick, ts_ns: int, cap: float) -> list[Fill]:
        """Fill only the levels that respect the limit price."""
        levels = tick.asks if o.side is Side.BUY else tick.bids
        remaining = o.remaining
        notional = 0.0
        got = 0
        for lvl in levels[: self.cfg.max_levels]:
            if remaining <= 0:
                break
            if o.side is Side.BUY and lvl.price > cap:
                break
            if o.side is Side.SELL and lvl.price < cap:
                break
            take = min(remaining, lvl.quantity)
            notional += take * lvl.price
            remaining -= take
            got += take
        if got <= 0:
            return []
        return [o._record(ts_ns, notional / got, got, maker=False)]

    def _rest(self, o: Order, tick: Tick, ts_ns: int, vol_delta: int) -> list[Fill]:
        """Sit in the queue at our limit and wait for the tape to reach us."""
        lp = o.limit_price
        assert lp is not None
        levels = tick.bids if o.side is Side.BUY else tick.asks

        # (Re)establish queue position when we first rest, or when our price
        # level vanished and came back — anyone who queued while we were not
        # displayed is ahead of us.
        if o.resting_price != lp:
            o.resting_price = lp
            o.queue_ahead = next(
                (l.quantity for l in levels if abs(l.price - lp) < 1e-9), 0
            )

        if vol_delta <= 0:
            return []

        through = (o.side is Side.BUY and tick.ltp < lp) or (
            o.side is Side.SELL and tick.ltp > lp
        )
        at_price = abs(tick.ltp - lp) < 1e-9

        if through:
            # Unambiguous: the tape printed past our limit, so our order was
            # in front of that trade. Still capped by what actually traded.
            qty = min(o.remaining, vol_delta)
            o.queue_ahead = 0
            return [o._record(ts_ns, lp, qty, maker=True)] if qty > 0 else []

        if not at_price:
            return []

        # Trading happened at our price. Some of it is on our side of the
        # book; `participation` is how much we dare claim.
        ours = int(vol_delta * self.cfg.participation)
        if ours <= 0:
            return []
        if o.queue_ahead >= ours:
            o.queue_ahead -= ours
            return []
        ours -= o.queue_ahead
        o.queue_ahead = 0
        qty = min(o.remaining, ours)
        return [o._record(ts_ns, lp, qty, maker=True)] if qty > 0 else []
