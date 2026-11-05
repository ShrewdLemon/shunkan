"""Multi-leg options backtest on end-of-day closes, one expiry at a time.

Prices are bhavcopy closes. A contract with no contracts traded still carries
NSE's stale close, so a leg counts only on a day it traded. Charges go through
`costs.py` on every fill; index options settle in cash at intrinsic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from shunkan.backtest import costs

RIGHTS = ("CE", "PE")


@dataclass(frozen=True)
class Leg:
    right: str      # "CE" | "PE"
    strike: float
    qty: int        # signed units per structure unit: +1 long, -1 short

    def __post_init__(self) -> None:
        if self.right not in RIGHTS:
            raise ValueError(f"right must be CE or PE, got {self.right!r}")
        if not self.qty or self.qty != int(self.qty):
            raise ValueError(f"qty must be a non-zero integer, got {self.qty!r}")
        object.__setattr__(self, "strike", float(self.strike))
        object.__setattr__(self, "qty", int(self.qty))


def _day(d) -> pd.Timestamp:
    return pd.Timestamp(d).normalize()


class DailyChain:
    """One symbol's option rows for one expiry, keyed by (day, strike, right)."""

    def __init__(self, frame: pd.DataFrame, expiry) -> None:
        self.expiry = _day(expiry)
        sub = frame[(frame["expiry"].dt.normalize() == self.expiry)
                    & frame["right"].isin(RIGHTS)]
        days = sub["date"].dt.normalize()
        strikes = sub["strike"].astype(float)
        keys = list(zip(days, strikes, sub["right"]))
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate (date, strike, right) rows: pass one symbol's chain")
        self._q = dict(zip(keys, zip(sub["close"].astype(float), sub["contracts"].astype(int))))
        self._k = {d: np.unique(g.to_numpy()) for d, g in strikes.groupby(days)}

    def quote(self, day, strike: float, right: str) -> tuple[float, int] | None:
        return self._q.get((_day(day), float(strike), right))

    def traded(self, day, strike: float, right: str) -> bool:
        q = self.quote(day, strike, right)
        return q is not None and q[1] > 0 and math.isfinite(q[0])

    def strikes(self, day) -> np.ndarray:
        return self._k.get(_day(day), np.empty(0)).copy()

    def days(self) -> list[pd.Timestamp]:
        return sorted(self._k)


def nearest_traded(chain: DailyChain, day, target: float, right: str, step: float = 50.0,
                   direction: int = 1, max_steps: int = 10, beyond: float | None = None
                   ) -> float | None:
    """First traded strike from `target` rounded to the grid, walking `direction`."""
    if direction not in (1, -1):
        raise ValueError("direction must be 1 or -1")
    k0 = round(target / step) * step
    for i in range(max_steps + 1):
        k = k0 + i * direction * step
        if beyond is not None and direction * (k - beyond) <= 0:
            continue
        if chain.traded(day, k, right):
            return float(k)
    return None


def parity_forward(chain: DailyChain, day, spot: float
                   ) -> tuple[float, float, float, float] | None:
    """(F, K, C, P) at the strike nearest spot with both sides traded; ties go low."""
    if not math.isfinite(spot):
        return None
    best = None
    for k in chain.strikes(day):
        if chain.traded(day, k, "CE") and chain.traded(day, k, "PE"):
            if best is None or abs(k - spot) < abs(best - spot):
                best = float(k)
    if best is None:
        return None
    c, p = chain.quote(day, best, "CE")[0], chain.quote(day, best, "PE")[0]
    return best + c - p, best, c, p


def _n(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black76_straddle(F: float, K: float, sigma: float, T: float) -> float:
    """Call plus put at K, Black-76 with r = 0."""
    sd = sigma * math.sqrt(T)
    if sd <= 0:
        return abs(F - K)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    call = F * _n(d1) - K * _n(d2)
    put = K * _n(-d2) - F * _n(-d1)
    return call + put


def straddle_vol(F: float, K: float, straddle: float, T: float) -> float:
    """Black-76 vol that prices the straddle, by bisection on [1e-4, 5]; NaN when none."""
    if not (F > 0 and K > 0 and T > 0 and math.isfinite(straddle)):
        return math.nan
    lo, hi = 1e-4, 5.0
    if straddle <= abs(F - K) or straddle >= black76_straddle(F, K, hi, T):
        return math.nan
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if black76_straddle(F, K, mid, T) < straddle:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def mark(chain: DailyChain, day, legs: list[Leg]) -> float | None:
    """Sum of qty * close, or None if any leg did not trade."""
    total = 0.0
    for leg in legs:
        if not chain.traded(day, leg.strike, leg.right):
            return None
        total += leg.qty * chain.quote(day, leg.strike, leg.right)[0]
    return total


def intrinsic(leg: Leg, settle: float) -> float:
    """Per-unit value at settlement, ignoring qty."""
    if leg.right == "CE":
        return max(settle - leg.strike, 0.0)
    return max(leg.strike - settle, 0.0)


@dataclass
class TradeResult:
    entry: pd.Timestamp
    exit_day: pd.Timestamp
    exit_kind: str          # "settle" | "take" | "stop"
    credit: float           # gross entry credit per unit = -sum(qty * entry_close)
    gross: float            # P&L per unit on closes/intrinsic, before slippage and charges
    slippage: float         # points per unit lost to slippage (>= 0)
    charges: float          # points per unit

    @property
    def net(self) -> float:
        return self.gross - self.slippage - self.charges


def _fill(qty: int, close: float, s: float, opening: bool) -> tuple[str, float]:
    if (qty > 0) == opening:
        return "BUY", close + s
    return "SELL", max(close - s, 0.0)


def simulate(chain: DailyChain, legs: list[Leg], entry, expiry, exit_days, settle: float,
             slippage: float, exit_rule=None, lot: int = 65) -> TradeResult:
    """Open at entry closes, close on the first exit-rule day with a full mark, else settle.

    Fills are charged through `costs.cost_of` at `lot * |qty|` units; settlement charges
    only exercise STT on ITM longs. Every per-unit figure is rupees / lot.
    """
    entry, expiry = _day(entry), _day(expiry)
    days = [_day(d) for d in exit_days]
    if expiry != chain.expiry or not entry < expiry:
        raise ValueError(f"expiry {expiry.date()} does not match the chain or entry")
    if any(not entry < d < expiry for d in days) or any(a >= b for a, b in zip(days, days[1:])):
        raise ValueError("exit_days must be increasing and strictly between entry and expiry")
    if not legs or not slippage >= 0 or not lot > 0:
        raise ValueError("need legs, slippage >= 0 and lot > 0")
    if mark(chain, entry, legs) is None:
        raise ValueError(f"a leg did not trade at entry {entry.date()}")

    opened = [chain.quote(entry, leg.strike, leg.right)[0] for leg in legs]
    credit = -sum(leg.qty * c for leg, c in zip(legs, opened))
    fills, cash = [], 0.0
    for leg, c in zip(legs, opened):
        side, px = _fill(leg.qty, c, slippage, True)
        fills.append(costs.Fill(side, premium=px, quantity=lot * abs(leg.qty)))
        cash -= leg.qty * px

    kind, day = "settle", expiry
    if exit_rule is not None:
        for d in days:
            m = mark(chain, d, legs)
            k = None if m is None else exit_rule(credit, -m)
            if k is not None:
                if k not in ("take", "stop"):
                    raise ValueError(f"exit_rule returned {k!r}")
                kind, day = k, d
                break

    rupees = 0.0
    if kind == "settle":
        if not math.isfinite(settle):
            raise ValueError(f"no settlement price for {expiry.date()}")
        values = [intrinsic(leg, settle) for leg in legs]
        cash += sum(leg.qty * v for leg, v in zip(legs, values))
        rupees += sum(costs.STT_EXERCISE_INTRINSIC * v * lot * leg.qty
                      for leg, v in zip(legs, values) if leg.qty > 0 and v > 0)
    else:
        values = [chain.quote(day, leg.strike, leg.right)[0] for leg in legs]
        for leg, c in zip(legs, values):
            side, px = _fill(leg.qty, c, slippage, False)
            fills.append(costs.Fill(side, premium=px, quantity=lot * abs(leg.qty)))
            cash += leg.qty * px
    gross = credit + sum(leg.qty * v for leg, v in zip(legs, values))
    rupees += costs.cost_of(fills).total
    return TradeResult(entry, day, kind, credit, gross, gross - cash, rupees / lot)


def take_profit(frac: float = 0.5):
    """Rule: "take" once the cost to close is at most frac * credit. Credit structures only."""
    def rule(credit: float, cost: float) -> str | None:
        return "take" if credit > 0 and cost <= frac * credit else None
    return rule


def take_and_stop(frac: float = 0.5, stop: float = 1.5):
    """take_profit, plus "stop" once cost - credit reaches stop * credit."""
    take = take_profit(frac)

    def rule(credit: float, cost: float) -> str | None:
        if credit > 0 and cost - credit >= stop * credit:
            return "stop"
        return take(credit, cost)
    return rule
