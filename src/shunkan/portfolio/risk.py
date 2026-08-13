"""Net book risk — what a desk actually watches.

A list of positions is not a risk picture. What matters is the book's net
delta, gamma, theta and vega: whether you are long or short the market, how
fast that flips, what you earn or bleed per day, and what a vol move does to
you. Those net out across legs — a short straddle is delta-flat and short
gamma, which no per-leg P&L column will ever tell you.

Marks come from the live chain for each (underlying, expiry) the book holds.
Any leg that cannot be marked is NAMED and excluded, never zero-filled: a net
delta that silently omits a leg is worse than no net delta at all.
"""

from __future__ import annotations

import numpy as np

from shunkan.derivatives.greeks import bs_greeks, implied_vol
from shunkan.portfolio.instrument import CE, EQ, FUT

GREEKS = ("delta", "gamma", "theta", "vega", "rho")


def _leg_greeks(inst, qty: float, chain) -> dict[str, float] | None:
    """Greeks for one position, scaled by signed quantity. None if unmarkable."""
    i = int(np.argmin(np.abs(chain.strikes - inst.strike)))
    if abs(float(chain.strikes[i]) - inst.strike) > 1e-6:
        return None  # the strike is not in this chain

    is_call = inst.kind == CE
    ltp = float(chain.call_ltp[i] if is_call else chain.put_ltp[i])
    iv = float(chain.call_iv[i] if is_call else chain.put_iv[i])
    if not np.isfinite(iv) or iv <= 0:
        # Chains that do not publish IV (Kite) get it solved from the premium.
        if ltp <= 0:
            return None
        iv = float(implied_vol(ltp, chain.spot, inst.strike, chain.t_years, is_call))
    if not np.isfinite(iv) or iv <= 0:
        return None

    g = bs_greeks(chain.spot, inst.strike, chain.t_years, iv, is_call)
    return {k: float(np.asarray(g[k]).ravel()[0]) * qty for k in GREEKS}


def book_greeks(positions, chains: dict) -> dict:
    """Net Greeks for a book.

    `chains` maps (symbol, expiry-string) to an OptionChain. Futures and cash
    contribute delta 1 per unit and nothing else; options are marked off their
    chain. Returns the net, a per-underlying breakdown, and the legs that
    could not be marked.
    """
    net = dict.fromkeys(GREEKS, 0.0)
    by_underlying: dict[str, dict[str, float]] = {}
    unmarked: list[str] = []

    for pos in positions:
        inst, qty = pos.instrument, pos.net_quantity
        if not qty:
            continue

        if inst.kind in (EQ, FUT):
            # Linear: one unit of delta per unit held, no convexity or decay.
            leg = dict.fromkeys(GREEKS, 0.0)
            leg["delta"] = qty
        else:
            chain = chains.get((inst.symbol, str(inst.expiry)))
            leg = _leg_greeks(inst, qty, chain) if chain is not None else None
            if leg is None:
                unmarked.append(inst.label)
                continue

        for k in GREEKS:
            net[k] += leg[k]
        bucket = by_underlying.setdefault(inst.symbol, dict.fromkeys(GREEKS, 0.0))
        for k in GREEKS:
            bucket[k] += leg[k]

    return {
        "net": net,
        "by_underlying": by_underlying,
        # Named, not silently dropped — the net below is incomplete without them.
        "unmarked": unmarked,
        "complete": not unmarked,
    }


def describe(net: dict) -> str:
    """One line a trader can read at a glance, in their own vocabulary."""
    bits = []
    d, g, t, v = net["delta"], net["gamma"], net["theta"], net["vega"]
    bits.append("delta-flat" if abs(d) < 1 else
                f"{'long' if d > 0 else 'short'} {abs(d):,.0f} delta")
    if abs(g) > 1e-9:
        bits.append(f"{'long' if g > 0 else 'short'} gamma")
    if abs(t) > 1e-9:
        bits.append(f"{'earning' if t > 0 else 'paying'} {abs(t):,.0f}/day")
    if abs(v) > 1e-9:
        bits.append(f"{'long' if v > 0 else 'short'} vega {abs(v):,.0f}")
    return " · ".join(bits)
