"""Backtest results, reported so the costs cannot hide.

Every headline number is given gross and net. A strategy whose gross edge is
large and net edge is negative has not "nearly worked" — it has discovered
that it is paying the exchange to trade, and the gap between the two figures
is exactly the thing to attack.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from shunkan.algo.charges import ChargeBreakdown


@dataclass
class RoundTrip:
    """One completed open-and-close, matched at average cost."""

    token: int
    opened_ns: int
    closed_ns: int
    quantity: int
    entry: float
    exit: float
    direction: int          # +1 long, -1 short
    gross: float
    charges: float

    @property
    def net(self) -> float:
        return self.gross - self.charges

    @property
    def points(self) -> float:
        return (self.exit - self.entry) * self.direction

    @property
    def held_seconds(self) -> float:
        return (self.closed_ns - self.opened_ns) / 1e9


@dataclass
class Result:
    strategy: str = ""
    session: str = ""
    ticks: int = 0
    orders: int = 0
    orders_rejected: int = 0
    fills: int = 0
    maker_fills: int = 0
    taker_fills: int = 0
    round_trips: list[RoundTrip] = field(default_factory=list)
    charges: ChargeBreakdown = field(default_factory=ChargeBreakdown)
    realized_gross: float = 0.0
    open_position: int = 0
    open_avg_price: float = 0.0
    mark_price: float = 0.0
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    risk: object | None = None   # RiskStats when a governor was fitted
    flattened_on_trip: bool = False
    # (ts_ns, token, side, price, quantity, is_maker), in fill order
    fill_log: list[tuple[int, int, str, float, int, bool]] = field(default_factory=list)

    # ---- headline ------------------------------------------------------

    @property
    def unrealized(self) -> float:
        if not self.open_position or not self.mark_price:
            return 0.0
        return (self.mark_price - self.open_avg_price) * self.open_position

    @property
    def gross_pnl(self) -> float:
        return self.realized_gross + self.unrealized

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.charges.total

    @property
    def cost_drag(self) -> float:
        """Fraction of gross profit consumed by charges. >1 means the costs
        exceeded the edge entirely."""
        return self.charges.total / self.gross_pnl if self.gross_pnl > 0 else math.inf

    @property
    def maker_ratio(self) -> float:
        return self.maker_fills / self.fills if self.fills else 0.0

    # ---- per-trade -----------------------------------------------------

    @property
    def wins(self) -> int:
        return sum(1 for t in self.round_trips if t.net > 0)

    @property
    def win_rate(self) -> float:
        return self.wins / len(self.round_trips) if self.round_trips else 0.0

    @property
    def avg_net_per_trade(self) -> float:
        n = len(self.round_trips)
        return sum(t.net for t in self.round_trips) / n if n else 0.0

    @property
    def avg_points_per_trade(self) -> float:
        n = len(self.round_trips)
        return sum(t.points for t in self.round_trips) / n if n else 0.0

    @property
    def expectancy_points(self) -> float:
        """Average points per round trip AFTER charges, expressed in points.

        This is the number to compare against `charges.breakeven_points`. If
        it is not comfortably above zero over hundreds of trades, there is no
        strategy here regardless of how the equity curve looks.
        """
        n = len(self.round_trips)
        if not n:
            return 0.0
        qty = sum(t.quantity for t in self.round_trips)
        return sum(t.net for t in self.round_trips) / qty if qty else 0.0

    @property
    def max_drawdown(self) -> float:
        peak = -math.inf
        dd = 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        return dd

    def summary(self) -> str:
        lines = [
            f"strategy        {self.strategy}",
            f"session         {self.session}",
            f"ticks           {self.ticks:,}",
            f"orders / fills  {self.orders:,} / {self.fills:,}"
            f"   (maker {self.maker_ratio:.0%})",
            f"risk-rejected   {self.orders_rejected:,}",
            f"round trips     {len(self.round_trips):,}",
            "",
            f"gross P&L       {self.gross_pnl:>14,.2f}",
            f"charges         {self.charges.total:>14,.2f}",
            f"NET P&L         {self.net_pnl:>14,.2f}",
        ]
        if self.gross_pnl > 0:
            lines.append(f"cost drag       {self.cost_drag:>14.1%} of gross")
        if self.round_trips:
            lines += [
                "",
                f"win rate        {self.win_rate:>14.1%}",
                f"avg net/trade   {self.avg_net_per_trade:>14,.2f}",
                f"expectancy      {self.expectancy_points:>14.4f} pts/unit  <- vs breakeven",
                f"max drawdown    {self.max_drawdown:>14,.2f}",
            ]
        if self.open_position:
            lines.append(
                f"\nOPEN at end     {self.open_position:+,} @ {self.open_avg_price:,.2f}"
                f"  (marked {self.mark_price:,.2f})"
            )
        breakdown = self.charges.as_dict()
        lines.append("\ncharges breakdown")
        for k in ("brokerage", "stt", "exchange", "ipft", "sebi", "stamp", "gst"):
            lines.append(f"  {k:<12} {breakdown[k]:>12,.2f}")
        return "\n".join(lines)
