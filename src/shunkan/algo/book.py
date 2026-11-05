"""Order-book arithmetic: what a given size would actually cost to execute.

The one question every execution model has to answer is "if I sent this now,
what price would I get?", and LTP cannot answer it. On today's NIFTY 24200 CE
the touch was 158.10 / 159.60 with only 65 lots on the bid: a 1,950-lot market
sell walks five levels and lands nowhere near the 158.25 the screen showed.
"""

from __future__ import annotations

from dataclasses import dataclass

from shunkan.stream.parser import DepthLevel, Tick


@dataclass(frozen=True)
class Sweep:
    """Result of walking `quantity` through one side of the book."""

    vwap: float           # average fill price, 0.0 if nothing filled
    filled: int           # quantity actually available
    levels: int           # price levels consumed
    exhausted: bool       # True if the visible book ran out before filling

    @property
    def complete(self) -> bool:
        return not self.exhausted


def walk(levels: list[DepthLevel], quantity: int) -> Sweep:
    """Consume `quantity` against price levels, best first.

    Returns what the *visible* book can do. Real books have hidden refresh —
    a resting order at level 6 you cannot see — so `exhausted=True` means
    "beyond what we can honestly model", not "impossible". The engine treats
    it as a partial fill rather than inventing liquidity.
    """
    if quantity <= 0 or not levels:
        return Sweep(0.0, 0, 0, quantity > 0)
    remaining = quantity
    notional = 0.0
    used = 0
    for lvl in levels:
        if remaining <= 0:
            break
        take = min(remaining, lvl.quantity)
        if take <= 0:
            continue
        notional += take * lvl.price
        remaining -= take
        used += 1
    filled = quantity - remaining
    if filled <= 0:
        return Sweep(0.0, 0, 0, True)
    return Sweep(notional / filled, filled, used, remaining > 0)


def sweep_buy(tick: Tick, quantity: int) -> Sweep:
    """Marketable buy lifts the offers."""
    return walk(tick.asks, quantity)


def sweep_sell(tick: Tick, quantity: int) -> Sweep:
    """Marketable sell hits the bids."""
    return walk(tick.bids, quantity)


def slippage(tick: Tick, quantity: int, is_buy: bool) -> float:
    """Points given up versus the midpoint by crossing for `quantity`.

    Half the touch spread is the floor; walking deeper adds to it. This is
    the number that decides whether a scalping signal is worth trading, and
    it is invisible in any candle-based backtest.
    """
    s = sweep_buy(tick, quantity) if is_buy else sweep_sell(tick, quantity)
    if s.filled <= 0:
        return 0.0
    mid = tick.mid
    return (s.vwap - mid) if is_buy else (mid - s.vwap)


def imbalance(tick: Tick, levels: int = 5) -> float:
    """Depth imbalance in [-1, 1]; positive means more bid than offer.

    A common microstructure feature. Kept here rather than in a strategy so
    every strategy computes it the same way and backtests stay comparable.
    """
    b = sum(l.quantity for l in tick.bids[:levels])
    a = sum(l.quantity for l in tick.asks[:levels])
    if b + a <= 0:
        return 0.0
    return (b - a) / (b + a)


def is_crossed(tick: Tick) -> bool:
    """Bid >= ask. Happens in real feeds during fast moves and at auction
    boundaries; any fill logic must refuse to trade on a crossed book rather
    than book the free money it appears to offer."""
    return bool(tick.bids and tick.asks and tick.bid >= tick.ask)


def is_tradeable(tick: Tick) -> bool:
    """Both sides present, positive prices, not crossed.

    The CAS window is exactly why this exists: between 15:15 and the auction
    print the cash index is frozen while F&O keeps trading, and around
    session boundaries feeds emit stale or one-sided books. Filling against
    one of those manufactures profit that never existed.
    """
    return bool(
        tick.bids and tick.asks
        and tick.bid > 0 and tick.ask > 0
        and not is_crossed(tick)
    )
