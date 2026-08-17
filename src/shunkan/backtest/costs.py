"""The Indian F&O cost stack, in rupees.

The backtester charged a flat commission rate, which is roughly the right shape
for equities and badly wrong for options. Two things dominate here and neither
scales the way a percentage does:

Brokerage is FLAT per executed order, so a four-legged structure pays eight
fixed charges regardless of size. On one lot that is a rounding error on a
Rs 4,488 credit; measured, it is 4.77% of it. At fifty lots the same eight
charges are 0.64%. This is why "the strategy works, just size down" is usually
backwards in Indian options.

STT on options is charged on the SELL side on premium, but on EXERCISED long
options it is charged on INTRINSIC value at a much higher rate. That is the
line that destroys people: an ITM long option allowed to expire is taxed on
its settlement value, not on the few rupees of premium it cost.

Rates below are the published Zerodha/exchange schedule as of August 2026 and
are declared as data, not buried in arithmetic, because they change and a
stale rate is a silently wrong backtest.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- published rates, August 2026 -------------------------------------------
# FLAT per executed order on F&O. Not "Rs 20 or 0.03%, whichever is lower" —
# that is the EQUITY INTRADAY rule, and applying it here quietly turns a Rs 20
# charge into Rs 1.17 on a one-lot leg. Understating cost is the same class of
# error as fabricating a price: it makes an unprofitable strategy look fine.
BROKERAGE_PER_ORDER = 20.0
STT_SELL_PREMIUM = 0.001          # 0.10% of premium, sell side only
STT_EXERCISE_INTRINSIC = 0.00125  # 0.125% of intrinsic on exercised longs
EXCHANGE_TXN_PREMIUM = 0.0003503  # NSE, on premium turnover
SEBI_TURNOVER = 0.000001          # Rs 10 per crore
STAMP_DUTY_BUY = 0.00003          # 0.003% of premium, buy side only
GST = 0.18                        # on brokerage + exchange + SEBI


@dataclass
class Fill:
    """One executed leg."""
    side: str          # BUY or SELL
    premium: float     # per unit
    quantity: int      # units, not lots
    intrinsic: float = 0.0   # per unit, only if allowed to expire ITM


@dataclass
class CostBreakdown:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return (self.brokerage + self.stt + self.exchange
                + self.sebi + self.stamp + self.gst)

    def as_pct_of(self, notional: float) -> float:
        return self.total / notional if notional else float("nan")


def cost_of(fills: list[Fill]) -> CostBreakdown:
    """Charge a list of executed legs. One brokerage per leg, always."""
    c = CostBreakdown()
    for f in fills:
        turnover = f.premium * f.quantity
        c.brokerage += BROKERAGE_PER_ORDER
        c.exchange += EXCHANGE_TXN_PREMIUM * turnover
        c.sebi += SEBI_TURNOVER * turnover
        if f.side.upper() == "SELL":
            c.stt += STT_SELL_PREMIUM * turnover
        else:
            c.stamp += STAMP_DUTY_BUY * turnover
        if f.intrinsic > 0:
            # The one that ruins people. Charged on settlement value, not on
            # what the option cost, and only on longs left to expire ITM.
            c.stt += STT_EXERCISE_INTRINSIC * f.intrinsic * f.quantity
    c.gst = GST * (c.brokerage + c.exchange + c.sebi)
    return c


def round_trip(legs: list[Fill]) -> CostBreakdown:
    """Open and close: every leg is executed twice, so charges double."""
    closing = [Fill(side="BUY" if f.side.upper() == "SELL" else "SELL",
                    premium=f.premium, quantity=f.quantity) for f in legs]
    opened, closed = cost_of(legs), cost_of(closing)
    return CostBreakdown(
        brokerage=opened.brokerage + closed.brokerage,
        stt=opened.stt + closed.stt,
        exchange=opened.exchange + closed.exchange,
        sebi=opened.sebi + closed.sebi,
        stamp=opened.stamp + closed.stamp,
        gst=opened.gst + closed.gst,
    )


def breakeven_edge(legs: list[Fill]) -> float:
    """How much gross edge, in premium terms, a structure needs to clear costs.

    Quote every backtest net of this. A strategy whose edge is smaller than
    its cost stack is not a small winner, it is a loser.
    """
    credit = sum(f.premium * f.quantity for f in legs if f.side.upper() == "SELL")
    return round_trip(legs).total / credit if credit else float("nan")
