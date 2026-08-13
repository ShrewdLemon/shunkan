"""Deterministic synthetic option chains for offline/demo mode.

Realistic shape: IV smile with put skew, OI concentrated at round strikes
near ATM (puts below, calls above), prices from Black-Scholes so the IV
solver round-trips cleanly in tests.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

import numpy as np

from shunkan.derivatives.chain import OptionChain
from shunkan.derivatives.greeks import bs_price

STRIKE_STEPS = {"NIFTY": 50.0, "BANKNIFTY": 100.0, "FINNIFTY": 50.0, "SENSEX": 100.0}
# No lot-size table lives here on purpose. NSE revises contract lots every few
# quarters — this table said NIFTY 75 / BANKNIFTY 35 long after the exchange
# moved them to 65 / 30 — and a stale lot is a silent multiplier on every rupee
# figure. Lots come from the NFO instruments dump (data.kite_fno.cached_lot_size)
# or not at all; a synthetic chain simply has none.


def synthetic_chain(
    symbol: str,
    spot: float | None = None,
    expiry: date | None = None,
    n_strikes: int = 21,
) -> OptionChain:
    sym = symbol.upper()
    seed = int(hashlib.sha1(f"chain:{sym}".encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)

    if spot is None:
        spot = {"NIFTY": 23200.0, "BANKNIFTY": 49800.0, "FINNIFTY": 23900.0}.get(
            sym, 500.0 + (seed % 3000)
        )
    if expiry is None:
        today = date.today()
        days_ahead = (3 - today.weekday()) % 7 or 7  # next Thursday
        expiry = today + timedelta(days=days_ahead)
    t_years = max((expiry - date.today()).days, 1) / 365.0

    step = STRIKE_STEPS.get(sym, max(round(spot * 0.025, -1), 5.0))
    atm = round(spot / step) * step
    half = n_strikes // 2
    strikes = atm + step * np.arange(-half, half + 1, dtype=np.float64)
    # Low-priced underlyings: drop non-positive strikes rather than emit
    # contracts that can't exist (negative strikes break log-moneyness).
    strikes = strikes[strikes >= step * 0.5]

    # IV smile: base level + put skew + mild convexity, small noise.
    moneyness = (strikes - spot) / spot
    base_iv = 0.13 + (seed % 7) * 0.01
    iv_call = base_iv + 1.4 * moneyness**2 - 0.10 * moneyness + rng.normal(0, 0.002, len(strikes))
    iv_put = iv_call + 0.012  # slight put premium
    iv_call = np.clip(iv_call, 0.08, 0.90)
    iv_put = np.clip(iv_put, 0.08, 0.90)

    call_ltp = np.round(bs_price(spot, strikes, t_years, iv_call, True), 2)
    put_ltp = np.round(bs_price(spot, strikes, t_years, iv_put, False), 2)

    # OI: gaussian mass near ATM; puts pile below spot, calls above.
    dist = np.abs(strikes - atm) / (step * half)
    shape = np.exp(-3.0 * dist**2)
    round_boost = np.where(np.isclose(strikes % (step * 5), 0.0), 1.8, 1.0)
    call_side = np.where(strikes >= spot, 1.4, 0.6)
    put_side = np.where(strikes <= spot, 1.4, 0.6)
    call_oi = np.round(shape * round_boost * call_side * rng.uniform(0.6, 1.4, len(strikes)) * 2.5e6, -3)
    put_oi = np.round(shape * round_boost * put_side * rng.uniform(0.6, 1.4, len(strikes)) * 2.5e6, -3)

    call_vol = np.round(call_oi * rng.uniform(0.1, 0.9, len(strikes)), -2)
    put_vol = np.round(put_oi * rng.uniform(0.1, 0.9, len(strikes)), -2)
    # A couple of unusual-activity strikes so the detector has something to find.
    hot = rng.choice(len(strikes), size=2, replace=False)
    call_vol[hot[0]] = call_oi[hot[0]] * 5.0 + 1000
    put_vol[hot[1]] = put_oi[hot[1]] * 4.0 + 1000

    call_oi_chg = np.round(call_oi * rng.normal(0.05, 0.15, len(strikes)), -2)
    put_oi_chg = np.round(put_oi * rng.normal(0.05, 0.15, len(strikes)), -2)

    return OptionChain(
        symbol=sym,
        spot=float(spot),
        expiry=expiry,
        t_years=t_years,
        strikes=strikes,
        call_ltp=call_ltp,
        call_oi=call_oi,
        call_oi_change=call_oi_chg,
        call_volume=call_vol,
        call_iv=iv_call,
        put_ltp=put_ltp,
        put_oi=put_oi,
        put_oi_change=put_oi_chg,
        put_volume=put_vol,
        put_iv=iv_put,
        source="synthetic (offline demo)",
        # No lot: a made-up chain has no contract behind it, and every
        # renderer prices per unit when lot_size is None.
        lot_size_source="no lot size — synthetic chain",
    )
