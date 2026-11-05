"""Risk governor: the layer with veto power over every order.

Nothing in this package may reach a broker without passing `check()` first.
That is the entire design: strategies propose, the governor disposes, and the
governor is the only component allowed to say no. A strategy cannot disable
it, raise its own limits, or route around it, because it never holds a
reference to anything that talks to a broker.

The governor is a state machine, not a filter:

    ARMED  -- orders flow, limits enforced per order
    TRIPPED -- a hard limit was breached. Every order is rejected. Cannot
               return to ARMED except through `rearm()`, which is a
               deliberate human act and is logged.

A tripped governor stays tripped for the rest of the session. That is
intentional and it is the most important line in this file. Automatic re-arm
after a loss limit is how an account that was down a controlled amount
becomes an account that is down all of it: the condition that caused the trip
is usually still present, and the strategy will re-enter into it immediately.

Every rejection carries a reason and is counted. A run that ends with a
thousand silent rejections is not a working system, and the summary makes
that visible rather than letting it hide behind a flat P&L.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import time as dtime
from enum import Enum
from pathlib import Path

from shunkan.algo.book import is_tradeable
from shunkan.algo.fills import Fill, Side
from shunkan.stream.parser import Tick


class RiskState(str, Enum):
    ARMED = "ARMED"
    TRIPPED = "TRIPPED"


class Reason(str, Enum):
    """Why an order was refused, or why the governor tripped."""

    OK = "OK"
    # Hard trips — these latch the governor.
    DAILY_LOSS = "DAILY_LOSS"
    DRAWDOWN = "DRAWDOWN"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    KILL_FILE = "KILL_FILE"
    MANUAL = "MANUAL"
    # Soft rejections — this order is refused, the next may be fine.
    POSITION_LIMIT = "POSITION_LIMIT"
    ORDER_SIZE = "ORDER_SIZE"
    ORDER_RATE = "ORDER_RATE"
    DAILY_ORDER_COUNT = "DAILY_ORDER_COUNT"
    PRICE_BAND = "PRICE_BAND"
    GROSS_EXPOSURE = "GROSS_EXPOSURE"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    BROKEN_BOOK = "BROKEN_BOOK"
    STALE_FEED = "STALE_FEED"
    TRIPPED = "TRIPPED"


HARD_TRIPS = {
    Reason.DAILY_LOSS,
    Reason.DRAWDOWN,
    Reason.CONSECUTIVE_LOSSES,
    Reason.KILL_FILE,
    Reason.MANUAL,
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: Reason = Reason.OK
    detail: str = ""

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Decision(True)


@dataclass(frozen=True)
class RiskLimits:
    """Every limit is required to have a value. There is no "unlimited".

    Defaults are deliberately small — a size you would be annoyed to lose,
    not one you cannot afford to. Raise them consciously, in code review,
    after the paper record justifies it.
    """

    # Money. Positive numbers; breaching any of these latches a trip.
    max_daily_loss: float = 5_000.0
    max_drawdown: float = 7_500.0
    max_consecutive_losses: int = 5

    # Size.
    max_position: int = 195            # 3 NIFTY lots
    max_order_qty: int = 65            # 1 lot
    max_gross_exposure: float = 200_000.0

    # Rate. SEBI treats >10 orders/sec as HFT requiring exchange approval;
    # staying under it is a compliance boundary, not a preference.
    max_orders_per_second: int = 8
    max_orders_per_minute: int = 120
    max_orders_per_day: int = 2_000    # Kite's own cap is 3,000

    # Sanity.
    price_band_pct: float = 0.05       # limit price within 5% of last traded
    stale_feed_seconds: float = 5.0
    require_tradeable_book: bool = True

    # Session window, local time. Orders outside it are refused. The default
    # close is 15:25 rather than 15:40: the last stretch before the auction is
    # when the cash reference freezes and the near-month future can move 25+
    # points with nothing to price against.
    session_open: dtime = dtime(9, 20)
    session_close: dtime = dtime(15, 25)

    # A file whose existence halts trading. Lets a human stop the system
    # without needing to find and kill the process.
    kill_file: Path | None = None

    # On trip, close the open position immediately at market.
    #
    # Default True, and the default matters. A trip stops NEW orders; without
    # this the position you already hold keeps marking against you, so a
    # 5,000 loss limit can settle well past 5,000 while the governor sits
    # there refusing orders it is no longer being asked for.
    #
    # The cost is that flattening crosses the spread, in a market that just
    # moved enough to trip you, which is the worst time to be the aggressor.
    # That is a real price and it is the right one to pay: the limit you set
    # should be close to the loss you actually take.
    flatten_on_trip: bool = True


_TIME_FIELDS = ("session_open", "session_close")
_PATH_FIELDS = ("kill_file",)


def limits_to_dict(limits: RiskLimits) -> dict:
    """JSON-safe: times as "HH:MM:SS", paths as str or None."""
    out = {}
    for f in fields(limits):
        v = getattr(limits, f.name)
        if f.name in _TIME_FIELDS:
            v = v.strftime("%H:%M:%S")
        elif f.name in _PATH_FIELDS:
            v = None if v is None else str(v)
        out[f.name] = v
    return out


def limits_from_dict(d: dict) -> RiskLimits:
    kw = dict(d)
    for k in _TIME_FIELDS:
        if k in kw:
            kw[k] = dtime.fromisoformat(kw[k])
    for k in _PATH_FIELDS:
        if kw.get(k) is not None:
            kw[k] = Path(kw[k])
    return RiskLimits(**kw)


@dataclass
class RiskStats:
    orders_checked: int = 0
    orders_allowed: int = 0
    orders_rejected: int = 0
    rejections: Counter = field(default_factory=Counter)
    tripped_at: float | None = None
    trip_reason: Reason | None = None
    trip_detail: str = ""
    peak_equity: float = 0.0
    worst_equity: float = 0.0
    consecutive_losses: int = 0

    def as_dict(self) -> dict:
        return {
            "orders_checked": self.orders_checked,
            "orders_allowed": self.orders_allowed,
            "orders_rejected": self.orders_rejected,
            "rejections": dict(self.rejections),
            "tripped_at": self.tripped_at,
            "trip_reason": self.trip_reason.value if self.trip_reason else None,
            "trip_detail": self.trip_detail,
            "peak_equity": round(self.peak_equity, 2),
            "worst_equity": round(self.worst_equity, 2),
        }


class RiskGovernor:
    """Holds the limits and the authority to refuse.

        gov = RiskGovernor(RiskLimits(max_daily_loss=2000))
        d = gov.check(token, Side.BUY, qty, limit, tick, position, ts_ns)
        if d:
            ...place the order...

    `observe_fill` and `observe_equity` must be called as they happen, or the
    money limits are enforced against stale state and will not fire in time.
    """

    def __init__(
        self,
        limits: RiskLimits | None = None,
        clock: "callable | None" = None,
    ) -> None:
        self.limits = limits or RiskLimits()
        self.state = RiskState.ARMED
        self.stats = RiskStats()
        self._clock = clock or time.time
        self._order_times: deque[float] = deque()
        self._orders_today = 0
        self._last_tick_ts: float | None = None
        self._equity = 0.0
        self._log: list[tuple[float, Reason, str]] = []

    # ---- state ---------------------------------------------------------

    @property
    def armed(self) -> bool:
        return self.state is RiskState.ARMED

    @property
    def tripped(self) -> bool:
        return self.state is RiskState.TRIPPED

    def trip(self, reason: Reason, detail: str = "") -> None:
        """Latch. Idempotent — the first reason is the one that is kept,
        because later breaches are usually consequences of the first."""
        if self.tripped:
            return
        self.state = RiskState.TRIPPED
        now = self._clock()
        self.stats.tripped_at = now
        self.stats.trip_reason = reason
        self.stats.trip_detail = detail
        self._log.append((now, reason, detail))

    def rearm(self, acknowledged_by: str) -> None:
        """Deliberate human act. Requires naming who is taking responsibility;
        an empty string is refused so this cannot be called reflexively in a
        retry loop."""
        if not acknowledged_by or not acknowledged_by.strip():
            raise ValueError(
                "rearm() requires a non-empty acknowledged_by — a human must "
                "own the decision to resume after a trip"
            )
        self._log.append((self._clock(), Reason.MANUAL, f"rearmed by {acknowledged_by}"))
        self.state = RiskState.ARMED
        self.stats.trip_reason = None
        self.stats.trip_detail = ""
        self.stats.tripped_at = None
        self.stats.consecutive_losses = 0

    @property
    def log(self) -> list[tuple[float, Reason, str]]:
        return list(self._log)

    def use_clock(self, fn: Callable[[], float]) -> None:
        """Replace the clock (epoch seconds) that window, rate and trip times read."""
        self._clock = fn

    # ---- observations ---------------------------------------------------

    def observe_tick(self, tick: Tick, ts_ns: int) -> None:
        self._last_tick_ts = ts_ns / 1e9

    def observe_equity(self, equity: float) -> None:
        """Net P&L including costs and open mark. Drives the money limits."""
        self._equity = equity
        self.stats.peak_equity = max(self.stats.peak_equity, equity)
        self.stats.worst_equity = min(self.stats.worst_equity, equity)
        lim = self.limits
        if equity <= -abs(lim.max_daily_loss):
            self.trip(
                Reason.DAILY_LOSS,
                f"net {equity:,.2f} breached daily loss limit {-abs(lim.max_daily_loss):,.2f}",
            )
            return
        dd = equity - self.stats.peak_equity
        if dd <= -abs(lim.max_drawdown):
            self.trip(
                Reason.DRAWDOWN,
                f"drawdown {dd:,.2f} from peak {self.stats.peak_equity:,.2f} "
                f"breached {-abs(lim.max_drawdown):,.2f}",
            )

    def observe_round_trip(self, net: float) -> None:
        if net < 0:
            self.stats.consecutive_losses += 1
            if self.stats.consecutive_losses >= self.limits.max_consecutive_losses:
                self.trip(
                    Reason.CONSECUTIVE_LOSSES,
                    f"{self.stats.consecutive_losses} losing trades in a row",
                )
        else:
            self.stats.consecutive_losses = 0

    def observe_fill(self, fill: Fill) -> None:
        """Present for symmetry and for future per-instrument limits; the
        money limits are driven by `observe_equity`."""
        return

    # ---- the gate -------------------------------------------------------

    def check(
        self,
        token: int,
        side: Side,
        quantity: int,
        limit_price: float | None,
        tick: Tick,
        position: int,
        ts_ns: int,
        now: float | None = None,
    ) -> Decision:
        self.stats.orders_checked += 1
        d = self._evaluate(token, side, quantity, limit_price, tick, position, ts_ns, now)
        if d.allowed:
            self.stats.orders_allowed += 1
            t = now if now is not None else self._clock()
            self._order_times.append(t)
            self._orders_today += 1
        else:
            self.stats.orders_rejected += 1
            self.stats.rejections[d.reason.value] += 1
            if d.reason in HARD_TRIPS:
                self.trip(d.reason, d.detail)
        return d

    def _evaluate(
        self, token, side, quantity, limit_price, tick, position, ts_ns, now
    ) -> Decision:
        lim = self.limits
        t = now if now is not None else self._clock()

        if lim.kill_file is not None and Path(lim.kill_file).exists():
            return Decision(False, Reason.KILL_FILE, f"{lim.kill_file} exists")

        if self.tripped:
            return Decision(
                False, Reason.TRIPPED,
                f"governor tripped: {self.stats.trip_reason} — {self.stats.trip_detail}",
            )

        if quantity <= 0 or quantity > lim.max_order_qty:
            return Decision(
                False, Reason.ORDER_SIZE,
                f"qty {quantity} outside 1..{lim.max_order_qty}",
            )

        projected = position + quantity * side.sign
        if abs(projected) > lim.max_position:
            return Decision(
                False, Reason.POSITION_LIMIT,
                f"would take position to {projected}, limit +/-{lim.max_position}",
            )

        if lim.require_tradeable_book and not is_tradeable(tick):
            return Decision(
                False, Reason.BROKEN_BOOK,
                f"book not tradeable (bid {tick.bid} ask {tick.ask})",
            )

        if self._last_tick_ts is not None:
            age = (ts_ns / 1e9) - self._last_tick_ts
            if age > lim.stale_feed_seconds:
                return Decision(
                    False, Reason.STALE_FEED, f"last tick {age:.1f}s ago",
                )

        ref = tick.ltp or tick.mid
        if limit_price is not None and ref > 0:
            drift = abs(limit_price - ref) / ref
            if drift > lim.price_band_pct:
                return Decision(
                    False, Reason.PRICE_BAND,
                    f"limit {limit_price:,.2f} is {drift:.1%} from last {ref:,.2f}",
                )

        exposure = abs(projected) * (limit_price or ref or 0.0)
        if exposure > lim.max_gross_exposure:
            return Decision(
                False, Reason.GROSS_EXPOSURE,
                f"exposure {exposure:,.0f} over {lim.max_gross_exposure:,.0f}",
            )

        window = self._window_check(t)
        if window is not None:
            return window

        rate = self._rate_check(t)
        if rate is not None:
            return rate

        return ALLOW

    def _window_check(self, t: float) -> Decision | None:
        lim = self.limits
        local = time.localtime(t)
        now_t = dtime(local.tm_hour, local.tm_min, local.tm_sec)
        if now_t < lim.session_open or now_t > lim.session_close:
            return Decision(
                False, Reason.OUTSIDE_WINDOW,
                f"{now_t.strftime('%H:%M:%S')} outside "
                f"{lim.session_open.strftime('%H:%M')}-{lim.session_close.strftime('%H:%M')}",
            )
        return None

    def _rate_check(self, t: float) -> Decision | None:
        lim = self.limits
        if self._orders_today >= lim.max_orders_per_day:
            return Decision(
                False, Reason.DAILY_ORDER_COUNT,
                f"{self._orders_today} orders today, cap {lim.max_orders_per_day}",
            )
        while self._order_times and t - self._order_times[0] > 60.0:
            self._order_times.popleft()
        in_minute = len(self._order_times)
        in_second = sum(1 for x in self._order_times if t - x <= 1.0)
        if in_second >= lim.max_orders_per_second:
            return Decision(
                False, Reason.ORDER_RATE,
                f"{in_second} orders in the last second, cap {lim.max_orders_per_second}",
            )
        if in_minute >= lim.max_orders_per_minute:
            return Decision(
                False, Reason.ORDER_RATE,
                f"{in_minute} orders in the last minute, cap {lim.max_orders_per_minute}",
            )
        return None

    # ---- reporting -------------------------------------------------------

    def summary(self) -> str:
        s = self.stats
        lines = [
            f"state           {self.state.value}",
            f"orders checked  {s.orders_checked:,}",
            f"  allowed       {s.orders_allowed:,}",
            f"  rejected      {s.orders_rejected:,}",
        ]
        if s.rejections:
            lines.append("rejections by reason")
            for reason, n in s.rejections.most_common():
                lines.append(f"  {reason:<20} {n:>8,}")
        if self.tripped:
            lines += [
                "",
                f"TRIPPED         {s.trip_reason.value if s.trip_reason else ''}",
                f"  {s.trip_detail}",
                "  will not resume without an explicit rearm()",
            ]
        lines += [
            "",
            f"peak equity     {s.peak_equity:>14,.2f}",
            f"worst equity    {s.worst_equity:>14,.2f}",
        ]
        return "\n".join(lines)
