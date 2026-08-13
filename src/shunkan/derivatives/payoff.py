"""Options strategy builder and payoff analysis.

A position is a list of Legs (buy/sell CE/PE at strikes, premium per unit).
Everything downstream is vectorized over a price grid: payoff-at-expiry
curve, breakevens, max profit/loss, probability of profit under the
lognormal implied by ATM IV, and aggregate position greeks.

Strategy templates are built from a live OptionChain so the strikes and
premiums are the actual tradable ones, not idealized numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from shunkan.derivatives.chain import OptionChain
from shunkan.derivatives.greeks import bs_greeks, norm_cdf


@dataclass
class Leg:
    side: int        # +1 long, -1 short
    kind: str        # "CE" | "PE"
    strike: float
    premium: float   # per unit
    iv: float = 0.15

    def payoff(self, expiry_px: np.ndarray) -> np.ndarray:
        if self.kind == "CE":
            intrinsic = np.maximum(expiry_px - self.strike, 0.0)
        else:
            intrinsic = np.maximum(self.strike - expiry_px, 0.0)
        return self.side * (intrinsic - self.premium)

    def describe(self) -> str:
        verb = "LONG" if self.side > 0 else "SHORT"
        return f"{verb} {self.strike:g} {self.kind} @ {self.premium:,.2f}"


@dataclass
class PayoffAnalysis:
    name: str
    symbol: str
    spot: float
    lot_size: int | None      # None when no source could name the contract lot
    legs: list[Leg]
    grid: np.ndarray          # expiry price grid
    payoff_per_unit: np.ndarray
    breakevens: list[float]
    # The money below is per lot when lot_size is known and per unit when it
    # is None — every renderer must read lot_size and label the unit it shows.
    max_profit: float         # per lot
    max_loss: float           # per lot
    net_premium: float        # per unit; >0 = credit received
    pop: float                # probability of profit at expiry (lognormal)
    greeks: dict[str, float] = field(default_factory=dict)  # per lot

    @property
    def risk_reward(self) -> float:
        if self.max_loss == 0:
            return float("inf")
        return abs(self.max_profit / self.max_loss)


def analyze_payoff(
    chain: OptionChain, legs: list[Leg], name: str = "custom", r: float = 0.065
) -> PayoffAnalysis:
    if not legs:
        raise ValueError("Position needs at least one leg")
    spot = chain.spot
    # Unknown lot -> per-unit money, and lot_size=None on the result tells
    # every renderer to say "per unit". Never multiply by a guessed lot.
    lot = chain.lot_size or 1

    # ±2.4σ price window: wide enough to show both wings settling, tight
    # enough that defined-risk structures don't look like a sliver.
    span = max(0.05, 2.4 * _atm_iv(chain) * np.sqrt(chain.t_years))
    grid = np.linspace(spot * (1 - span), spot * (1 + span), 1201)
    payoff = np.sum([leg.payoff(grid) for leg in legs], axis=0)

    breakevens = _zero_crossings(grid, payoff)
    # Max P/L on the grid; flag unbounded sides via the edge slope. Compare
    # against the per-step move of a single unit position (Δprice) so a flat
    # plateau (defined-risk wings) isn't mistaken for an open tail by float
    # noise — a real open tail moves ~Δprice per step, ~1e9× the roundoff.
    max_profit = float(payoff.max()) * lot
    max_loss = float(payoff.min()) * lot
    eps = (grid[1] - grid[0]) * 1e-3  # 0.1% of a price step
    if payoff[-1] - payoff[-2] > eps:  # rising at the right edge → unlimited upside
        max_profit = float("inf")
    if payoff[-2] - payoff[-1] > eps:
        max_loss = float("-inf")
    if payoff[0] - payoff[1] > eps:    # rising toward the left edge (puts)
        max_profit = float("inf")
    if payoff[1] - payoff[0] > eps:
        max_loss = float("-inf")

    net_premium = float(sum(-leg.side * leg.premium for leg in legs))
    pop = _probability_of_profit(chain, grid, payoff)

    g_total = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for leg in legs:
        g = bs_greeks(
            spot, np.array([leg.strike]), chain.t_years,
            np.array([max(leg.iv, 0.01)]), leg.kind == "CE", r,
        )
        for key in g_total:
            g_total[key] += leg.side * float(g[key][0]) * lot

    return PayoffAnalysis(
        name=name,
        symbol=chain.symbol,
        spot=spot,
        lot_size=chain.lot_size,
        legs=legs,
        grid=grid,
        payoff_per_unit=payoff,
        breakevens=breakevens,
        max_profit=max_profit,
        max_loss=max_loss,
        net_premium=net_premium,
        pop=pop,
        greeks=g_total,
    )


def _atm_iv(chain: OptionChain) -> float:
    chain.ensure_iv()
    i = chain.atm_index
    ivs = [v for v in (chain.call_iv[i], chain.put_iv[i]) if not np.isnan(v)]
    return float(np.mean(ivs)) if ivs else 0.15


def _zero_crossings(grid: np.ndarray, payoff: np.ndarray) -> list[float]:
    sign = np.sign(payoff)
    flips = np.flatnonzero(np.diff(sign) != 0)
    out = []
    for i in flips:
        x0, x1 = grid[i], grid[i + 1]
        y0, y1 = payoff[i], payoff[i + 1]
        if y1 != y0:
            out.append(float(x0 - y0 * (x1 - x0) / (y1 - y0)))
    return out


def _probability_of_profit(
    chain: OptionChain, grid: np.ndarray, payoff: np.ndarray
) -> float:
    """P(payoff > 0) at expiry under lognormal terminal prices with ATM IV.

    A model estimate, not a market-implied density — close enough to rank
    strategies, wrong enough to keep the label honest in the UI.
    """
    sigma = _atm_iv(chain)
    t = max(chain.t_years, 1e-6)
    s = sigma * np.sqrt(t)
    if s <= 0:
        return 0.0
    mu = np.log(chain.spot) - 0.5 * s * s  # risk-neutral drift ≈ 0 over days

    profitable = payoff > 0
    edges = np.concatenate([[grid[0]], 0.5 * (grid[1:] + grid[:-1]), [grid[-1]]])
    z_hi = (np.log(np.maximum(edges[1:], 1e-12)) - mu) / s
    z_lo = (np.log(np.maximum(edges[:-1], 1e-12)) - mu) / s
    bin_prob = norm_cdf(z_hi) - norm_cdf(z_lo)
    pop = float((bin_prob * profitable).sum())
    # Tails beyond the grid: attribute to the edge bins' profitability.
    left_tail = float(norm_cdf((np.log(grid[0]) - mu) / s))
    right_tail = float(1.0 - norm_cdf((np.log(grid[-1]) - mu) / s))
    pop += left_tail * bool(profitable[0]) + right_tail * bool(profitable[-1])
    return min(max(pop, 0.0), 1.0)


# ---------------------------------------------------------------------------
# Strategy templates built from a live chain
# ---------------------------------------------------------------------------


def _leg_from_chain(chain: OptionChain, idx: int, kind: str, side: int) -> Leg:
    if kind == "CE":
        premium = float(chain.call_ltp[idx])
        iv = float(np.nan_to_num(chain.call_iv[idx], nan=0.15))
    else:
        premium = float(chain.put_ltp[idx])
        iv = float(np.nan_to_num(chain.put_iv[idx], nan=0.15))
    return Leg(side=side, kind=kind, strike=float(chain.strikes[idx]), premium=premium, iv=iv)


def _offset_index(chain: OptionChain, offset: int) -> int:
    return int(np.clip(chain.atm_index + offset, 0, len(chain.strikes) - 1))


def build_strategy(chain: OptionChain, name: str, width: int = 2) -> PayoffAnalysis:
    """Construct a named strategy from real chain prices.

    width = strikes away from ATM for the 'far' legs (wings/short strikes).
    """
    key = name.lower().replace("-", "_")
    atm = chain.atm_index
    w = max(width, 1)

    if key in ("long_straddle", "straddle"):
        legs = [_leg_from_chain(chain, atm, "CE", +1),
                _leg_from_chain(chain, atm, "PE", +1)]
    elif key == "short_straddle":
        legs = [_leg_from_chain(chain, atm, "CE", -1),
                _leg_from_chain(chain, atm, "PE", -1)]
    elif key in ("long_strangle", "strangle"):
        legs = [_leg_from_chain(chain, _offset_index(chain, +w), "CE", +1),
                _leg_from_chain(chain, _offset_index(chain, -w), "PE", +1)]
    elif key == "short_strangle":
        legs = [_leg_from_chain(chain, _offset_index(chain, +w), "CE", -1),
                _leg_from_chain(chain, _offset_index(chain, -w), "PE", -1)]
    elif key == "bull_call_spread":
        legs = [_leg_from_chain(chain, atm, "CE", +1),
                _leg_from_chain(chain, _offset_index(chain, +w), "CE", -1)]
    elif key == "bear_put_spread":
        legs = [_leg_from_chain(chain, atm, "PE", +1),
                _leg_from_chain(chain, _offset_index(chain, -w), "PE", -1)]
    elif key == "iron_condor":
        legs = [
            _leg_from_chain(chain, _offset_index(chain, +w), "CE", -1),
            _leg_from_chain(chain, _offset_index(chain, +2 * w), "CE", +1),
            _leg_from_chain(chain, _offset_index(chain, -w), "PE", -1),
            _leg_from_chain(chain, _offset_index(chain, -2 * w), "PE", +1),
        ]
    elif key == "iron_fly":
        legs = [
            _leg_from_chain(chain, atm, "CE", -1),
            _leg_from_chain(chain, atm, "PE", -1),
            _leg_from_chain(chain, _offset_index(chain, +2 * w), "CE", +1),
            _leg_from_chain(chain, _offset_index(chain, -2 * w), "PE", +1),
        ]
    else:
        raise KeyError(
            f"Unknown strategy '{name}'. Choices: {', '.join(sorted(PAYOFF_STRATEGIES))}"
        )
    return analyze_payoff(chain, legs, name=key)


PAYOFF_STRATEGIES = (
    "long_straddle", "short_straddle", "long_strangle", "short_strangle",
    "bull_call_spread", "bear_put_spread", "iron_condor", "iron_fly",
)


def parse_custom_legs(chain: OptionChain, tokens: list[str]) -> list[Leg]:
    """Parse `+23200CE -23400CE` style leg specs against chain premiums."""
    legs = []
    for token in tokens:
        t = token.strip().upper()
        if not t or t[0] not in "+-" or t[-2:] not in ("CE", "PE"):
            raise ValueError(
                f"Bad leg '{token}'. Format: +23200CE (long) or -23400PE (short)"
            )
        side = +1 if t[0] == "+" else -1
        kind = t[-2:]
        try:
            strike = float(t[1:-2])
        except ValueError as exc:
            raise ValueError(f"Bad strike in '{token}'") from exc
        matches = np.flatnonzero(np.isclose(chain.strikes, strike))
        if len(matches) == 0:
            raise ValueError(
                f"Strike {strike:g} not in chain "
                f"({chain.strikes[0]:g}–{chain.strikes[-1]:g} step {chain.strikes[1]-chain.strikes[0]:g})"
            )
        legs.append(_leg_from_chain(chain, int(matches[0]), kind, side))
    return legs
