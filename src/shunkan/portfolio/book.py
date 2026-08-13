"""A book that can be short.

Every lot records the side it was opened on. A trade first offsets opposing
lots FIFO — realizing P&L — and whatever is left over opens on the new side.
That is how a broker's book behaves, and it removes the ambiguity a signed
quantity has: "SELL 2" while long 1 unambiguously closes the 1 and opens a
short 1, with no caller-supplied intent flag to get wrong.

Quantities are in UNITS, not lots. F&O is *traded* in lots, so `trade_lots`
converts at the boundary — but P&L arithmetic is per unit, and a book that
stored lots would silently produce nonsense the day a lot size is revised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from shunkan.portfolio.instrument import Instrument

BUY = "BUY"
SELL = "SELL"

_EPS = 1e-9


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Lot:
    side: str          # BUY or SELL — the side this lot was OPENED on
    quantity: float    # always positive; direction lives in `side`
    price: float
    timestamp: str = field(default_factory=_now)

    @property
    def signed(self) -> float:
        return self.quantity if self.side == BUY else -self.quantity


@dataclass
class BookPosition:
    instrument: Instrument
    lots: list[Lot] = field(default_factory=list)

    @property
    def net_quantity(self) -> float:
        """Signed: positive is long, negative is short."""
        return sum(l.signed for l in self.lots)

    @property
    def quantity(self) -> float:
        """Alias for net_quantity. Long-only callers predate the short side
        and read the same number; a short simply reads negative."""
        return self.net_quantity

    @property
    def is_short(self) -> bool:
        return self.net_quantity < -_EPS

    @property
    def avg_cost(self) -> float:
        """Average price of the open side. Zero for a flat position."""
        net = self.net_quantity
        if abs(net) <= _EPS:
            return 0.0
        side = BUY if net > 0 else SELL
        open_lots = [l for l in self.lots if l.side == side]
        qty = sum(l.quantity for l in open_lots)
        if qty <= _EPS:
            return 0.0
        return sum(l.quantity * l.price for l in open_lots) / qty

    def market_value(self, price: float) -> float:
        """Signed exposure. A short position is a liability, so it is negative."""
        return self.net_quantity * price

    def unrealized_pnl(self, price: float) -> float:
        """Works for both directions: a short gains as price falls, because
        net_quantity is negative and so is (price - avg_cost) * net."""
        return (price - self.avg_cost) * self.net_quantity


class Book:
    """Sided position ledger. Cash and margin live on the owning Portfolio."""

    def __init__(self) -> None:
        self.positions: dict[str, BookPosition] = {}

    def get(self, instrument: Instrument) -> BookPosition | None:
        return self.positions.get(instrument.key)

    def net_quantity(self, instrument: Instrument) -> float:
        pos = self.get(instrument)
        return pos.net_quantity if pos else 0.0

    def trade(self, instrument: Instrument, side: str, quantity: float,
              price: float) -> tuple[float, float, float]:
        """Record one fill.

        Returns (realized_pnl, closed_qty, opened_qty). The split matters to
        the caller: closing a short *costs* cash while opening one *credits*
        premium, and a single fill can do both.
        """
        side = side.upper()
        if side not in (BUY, SELL):
            raise ValueError(f"side must be {BUY} or {SELL}, got {side!r}")
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        pos = self.positions.setdefault(
            instrument.key, BookPosition(instrument=instrument))
        opposite = SELL if side == BUY else BUY

        remaining, realized, closed = quantity, 0.0, 0.0
        # 1. Offset opposing lots oldest-first.
        while remaining > _EPS:
            victim = next((l for l in pos.lots if l.side == opposite), None)
            if victim is None:
                break
            take = min(victim.quantity, remaining)
            # Long closed by a sell earns (exit - entry); short closed by a
            # buy earns (entry - exit). One expression, both directions.
            realized += (price - victim.price) * take * (1 if opposite == BUY else -1)
            victim.quantity -= take
            remaining -= take
            closed += take
            if victim.quantity <= _EPS:
                pos.lots.remove(victim)

        # 2. Whatever is left opens on the new side.
        if remaining > _EPS:
            pos.lots.append(Lot(side=side, quantity=remaining, price=price))

        if not pos.lots:
            del self.positions[instrument.key]
        return realized, closed, remaining

    def market_value(self, prices: dict[str, float]) -> float:
        return sum(p.market_value(prices.get(k, p.avg_cost))
                   for k, p in self.positions.items())

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        return sum(p.unrealized_pnl(prices.get(k, p.avg_cost))
                   for k, p in self.positions.items())

    def expired(self, when=None) -> list[BookPosition]:
        """Positions whose contract has stopped trading. Surfaced rather than
        auto-settled: assignment is a real cash event and inventing one would
        be a fabricated number."""
        return [p for p in self.positions.values() if p.instrument.expired(when)]
