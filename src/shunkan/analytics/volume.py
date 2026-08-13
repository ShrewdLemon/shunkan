"""Volume pattern detection — profile, surges, OBV divergence, A/D reads.

All vectorized numpy over OHLCV arrays; the full panel analysis on a year
of daily bars runs in well under a millisecond.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class VolumeProfile:
    bin_edges: np.ndarray     # (bins+1,)
    volume_at_price: np.ndarray  # (bins,)
    poc: float                # point of control — price with max traded volume
    value_area_low: float     # 70% volume band
    value_area_high: float


@dataclass
class VolumeReport:
    surge_z: float            # today's volume z-score vs trailing 20 bars
    surge_ratio: float        # today / 20-bar average
    obv_divergence: str       # "bearish divergence" / "bullish divergence" / "none"
    day_type: str             # accumulation / distribution / churn / quiet
    profile: VolumeProfile | None = None
    notes: list[str] = field(default_factory=list)


def _col(df: pd.DataFrame, name: str) -> np.ndarray:
    cols = {c.lower(): c for c in df.columns}
    return df[cols[name]].to_numpy(dtype=np.float64)


def volume_profile(ohlcv: pd.DataFrame, bins: int = 24) -> VolumeProfile:
    """Distribute each bar's volume across the price bins its range spans."""
    high, low, vol = _col(ohlcv, "high"), _col(ohlcv, "low"), _col(ohlcv, "volume")
    lo, hi = float(np.nanmin(low)), float(np.nanmax(high))
    if hi <= lo:
        hi = lo + 1e-9
    edges = np.linspace(lo, hi, bins + 1)

    # Overlap of [low_i, high_i] with each bin, as a fraction of bar range —
    # one (bars x bins) broadcast, no Python loop.
    bar_lo = low[:, None]
    bar_hi = high[:, None]
    seg_lo = np.maximum(bar_lo, edges[None, :-1])
    seg_hi = np.minimum(bar_hi, edges[None, 1:])
    overlap = np.clip(seg_hi - seg_lo, 0.0, None)
    bar_range = np.maximum(bar_hi - bar_lo, 1e-12)
    vap = (overlap / bar_range * vol[:, None]).sum(axis=0)

    poc_i = int(np.argmax(vap))
    poc = float(0.5 * (edges[poc_i] + edges[poc_i + 1]))

    # Value area: expand around POC until 70% of volume is inside.
    target = 0.70 * vap.sum()
    inside = {poc_i}
    acc = vap[poc_i]
    lo_i, hi_i = poc_i, poc_i
    while acc < target and (lo_i > 0 or hi_i < bins - 1):
        below = vap[lo_i - 1] if lo_i > 0 else -1.0
        above = vap[hi_i + 1] if hi_i < bins - 1 else -1.0
        if above >= below:
            hi_i += 1
            acc += vap[hi_i]
            inside.add(hi_i)
        else:
            lo_i -= 1
            acc += vap[lo_i]
            inside.add(lo_i)

    return VolumeProfile(
        bin_edges=edges,
        volume_at_price=vap,
        poc=poc,
        value_area_low=float(edges[lo_i]),
        value_area_high=float(edges[hi_i + 1]),
    )


def obv(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    direction = np.sign(np.diff(close, prepend=close[0]))
    return np.cumsum(direction * volume)


def detect_obv_divergence(close: np.ndarray, volume: np.ndarray, window: int = 40) -> str:
    """Price/OBV disagreement over the recent window."""
    if len(close) < window + 2:
        return "none"
    c = close[-window:]
    o = obv(close, volume)[-window:]
    half = window // 2
    price_trend = np.sign(c[half:].mean() - c[:half].mean())
    obv_trend = np.sign(o[half:].mean() - o[:half].mean())
    if price_trend > 0 and obv_trend < 0:
        return "bearish divergence (price up, volume flow down)"
    if price_trend < 0 and obv_trend > 0:
        return "bullish divergence (price down, volume flow up)"
    return "none"


def classify_day(ohlcv: pd.DataFrame) -> tuple[str, float, float]:
    """Label the latest bar by close-location-value and relative volume."""
    high, low, close, vol = (_col(ohlcv, c) for c in ("high", "low", "close", "volume"))
    rng = max(high[-1] - low[-1], 1e-12)
    clv = (2 * close[-1] - low[-1] - high[-1]) / rng  # -1 bottom .. +1 top
    trail = vol[-21:-1] if len(vol) > 21 else vol[:-1]
    avg = trail.mean() if len(trail) else vol[-1]
    sd = trail.std(ddof=1) if len(trail) > 2 else 0.0
    z = (vol[-1] - avg) / sd if sd > 0 else 0.0
    ratio = vol[-1] / avg if avg > 0 else 1.0

    heavy = ratio > 1.5
    if heavy and clv > 0.3:
        label = "accumulation (heavy volume, close near high)"
    elif heavy and clv < -0.3:
        label = "distribution (heavy volume, close near low)"
    elif heavy:
        label = "churn (heavy volume, indecisive close)"
    elif ratio < 0.6:
        label = "quiet (volume drying up)"
    else:
        label = "normal"
    return label, float(z), float(ratio)


def analyze_volume(ohlcv: pd.DataFrame, profile_bins: int = 24) -> VolumeReport:
    close, vol = _col(ohlcv, "close"), _col(ohlcv, "volume")
    day_type, z, ratio = classify_day(ohlcv)
    divergence = detect_obv_divergence(close, vol)
    prof = volume_profile(ohlcv.tail(120), bins=profile_bins)

    notes: list[str] = []
    if ratio > 2.0:
        notes.append(f"volume {ratio:.1f}× its 20-bar average — institutional footprint")
    if divergence != "none":
        notes.append(divergence)
    last = float(close[-1])
    if last > prof.value_area_high:
        notes.append(f"trading above value area ({prof.value_area_high:,.0f}) — acceptance or rejection pending")
    elif last < prof.value_area_low:
        notes.append(f"trading below value area ({prof.value_area_low:,.0f}) — weak hands testing")
    if abs(last - prof.poc) / max(last, 1e-9) < 0.005:
        notes.append(f"sitting on POC {prof.poc:,.0f} — high-volume node acts as magnet")

    return VolumeReport(
        surge_z=z,
        surge_ratio=ratio,
        obv_divergence=divergence,
        day_type=day_type,
        profile=prof,
        notes=notes,
    )
