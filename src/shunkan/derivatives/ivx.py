"""Volatility intelligence: realized vol, IV vs RV, smile, expected-move cone.

For premium sellers/buyers the core question is "is implied vol rich or
cheap versus what the underlying actually does?" This module answers it
with transparent estimators:

- Realized vol: close-to-close and Parkinson (high/low range — ~5x more
  efficient on the same window).
- IV premium: ATM IV minus realized, in vol points. Positive = options
  priced rich vs recent movement (seller's tailwind, buyer's headwind).
- RV percentile: where today's realized vol sits in its own 1y history —
  an honest stand-in for IV rank when no IV history exists.
- Expected-move cone: ±1σ/±2σ price bands from ATM IV over the days ahead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from shunkan.derivatives.chain import OptionChain

TRADING_DAYS = 252


def realized_vol_cc(close: pd.Series, window: int = 21) -> pd.Series:
    """Annualized close-to-close realized volatility."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(window, min_periods=window).std(ddof=1) * np.sqrt(TRADING_DAYS)


def realized_vol_parkinson(high: pd.Series, low: pd.Series, window: int = 21) -> pd.Series:
    """Parkinson range-based realized vol (uses intrabar high/low)."""
    factor = 1.0 / (4.0 * np.log(2.0))
    rng = np.log(high / low) ** 2
    return np.sqrt(factor * rng.rolling(window, min_periods=window).mean() * TRADING_DAYS)


@dataclass
class VolReport:
    symbol: str
    spot: float
    atm_iv: float
    rv_cc_21: float
    rv_park_21: float
    iv_premium: float        # atm_iv - rv_cc_21, in vol points
    rv_percentile: float     # today's RV vs its own 1y distribution
    cone: dict[int, tuple[float, float, float, float]] = field(default_factory=dict)
    # days -> (-2σ, -1σ, +1σ, +2σ) price levels
    smile_strikes: np.ndarray | None = None
    smile_call_iv: np.ndarray | None = None
    smile_put_iv: np.ndarray | None = None
    notes: list[str] = field(default_factory=list)


def analyze_vol(chain: OptionChain, history: pd.DataFrame) -> VolReport:
    chain.ensure_iv()
    close = history["close"]
    rv_cc = realized_vol_cc(close)
    rv_park = realized_vol_parkinson(history["high"], history["low"])

    rv_now = float(rv_cc.dropna().iloc[-1]) if len(rv_cc.dropna()) else float("nan")
    park_now = float(rv_park.dropna().iloc[-1]) if len(rv_park.dropna()) else float("nan")

    i = chain.atm_index
    ivs = [v for v in (chain.call_iv[i], chain.put_iv[i]) if not np.isnan(v)]
    atm_iv = float(np.mean(ivs)) if ivs else float("nan")

    rv_hist = rv_cc.dropna()
    pct = float((rv_hist <= rv_now).mean()) if len(rv_hist) > 20 else float("nan")

    premium = atm_iv - rv_now if not (np.isnan(atm_iv) or np.isnan(rv_now)) else float("nan")

    cone = {}
    spot = chain.spot
    if not np.isnan(atm_iv):
        for days in (1, 3, 5, 10, 21):
            s = atm_iv * np.sqrt(days / TRADING_DAYS)
            cone[days] = (
                spot * np.exp(-2 * s), spot * np.exp(-s),
                spot * np.exp(s), spot * np.exp(2 * s),
            )

    notes = []
    if not np.isnan(premium):
        if premium > 0.04:
            notes.append(
                f"IV {premium * 100:+.1f} vol pts over realized — options rich; "
                "premium selling has a statistical tailwind (gap risk remains)"
            )
        elif premium < -0.02:
            notes.append(
                f"IV {premium * 100:+.1f} vol pts under realized — options cheap "
                "vs actual movement; long-vol structures are underpriced"
            )
        else:
            notes.append("IV roughly fair vs realized movement")
    if not np.isnan(pct):
        if pct > 0.8:
            notes.append(f"Realized vol in its {pct:.0%} percentile (1y) — elevated regime")
        elif pct < 0.2:
            notes.append(f"Realized vol in its {pct:.0%} percentile (1y) — compressed; expansions start here")
    if not np.isnan(park_now) and not np.isnan(rv_now) and park_now > rv_now * 1.3:
        notes.append("Parkinson vol >> close-close vol — big intraday ranges that close back; chop, not trend")

    return VolReport(
        symbol=chain.symbol,
        spot=spot,
        atm_iv=atm_iv,
        rv_cc_21=rv_now,
        rv_park_21=park_now,
        iv_premium=premium,
        rv_percentile=pct,
        cone=cone,
        smile_strikes=chain.strikes,
        smile_call_iv=chain.call_iv,
        smile_put_iv=chain.put_iv,
        notes=notes,
    )
