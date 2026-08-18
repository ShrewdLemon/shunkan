"""Candlestick patterns, with their measured record attached.

Every charting site in India will tell you AMBER printed a Bullish Harami
today. None of them will tell you what a Bullish Harami on AMBER has been
worth, because the honest answer is usually "nothing distinguishable from
any other day" - and that answer kills the feature everywhere except here.

So: deterministic pattern definitions (each formula in its docstring, every
threshold a named constant), and next to every detection the archive's own
verdict - occurrences, forward returns at +1/+3/+5 closes, hit rate, and
the any-day baseline beside each number. A pattern with n=7 says n=7. A
pattern whose mean is indistinguishable from baseline says so. The shapes
are real; whether they MEAN anything is an empirical question this module
answers per symbol instead of assuming.

Multi-day forward windows overlap when patterns cluster; stated, not
corrected - these are descriptive base rates, not a backtest.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Thresholds, named so the definitions are inspectable rather than folklore.
DOJI_BODY_MAX = 0.10          # body <= 10% of range
MARUBOZU_BODY_MIN = 0.90      # body >= 90% of range
SHADOW_RATIO = 2.0            # hammer/star: long shadow >= 2x body
SMALL_SHADOW_MAX = 0.25       # ...and the other shadow <= 25% of body
STAR_GAP_BODY = 0.30          # star middle body <= 30% of neighbours' mean
HORIZONS = (1, 3, 5)


@dataclass(frozen=True)
class Detection:
    pattern: str
    direction: str      # bullish | bearish | neutral
    date: str           # ISO date of the completing candle


def _cols(df: pd.DataFrame):
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    return o, h, l, c


def detect_all(df: pd.DataFrame) -> pd.DataFrame:
    """Every pattern occurrence in a daily OHLC frame (index: dates).

    Returns a frame with columns pattern/direction indexed by date. A day
    can print several patterns; most days honestly print none.
    """
    o, h, l, c = _cols(df)
    n = len(df)
    rng = h - l
    body = np.abs(c - o)
    up = c > o
    dn = c < o
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    safe_rng = np.where(rng > 0, rng, np.nan)
    safe_body = np.where(body > 0, body, np.nan)

    hits: list[tuple[int, str, str]] = []

    def mark(mask, pattern, direction):
        for i in np.flatnonzero(mask):
            hits.append((i, pattern, direction))

    mark((body / safe_rng <= DOJI_BODY_MAX) & (rng > 0), "doji", "neutral")
    mark((body / safe_rng >= MARUBOZU_BODY_MIN) & up, "bullish marubozu", "bullish")
    mark((body / safe_rng >= MARUBOZU_BODY_MIN) & dn, "bearish marubozu", "bearish")
    # hammer / shooting star: long shadow one side, tiny the other
    mark((lower >= SHADOW_RATIO * safe_body) & (upper <= SMALL_SHADOW_MAX * safe_body),
         "hammer", "bullish")
    mark((upper >= SHADOW_RATIO * safe_body) & (lower <= SMALL_SHADOW_MAX * safe_body),
         "shooting star", "bearish")

    if n >= 2:
        po, pc = o[:-1], c[:-1]
        prev_up, prev_dn = pc > po, pc < po
        cur = slice(1, None)
        # engulfing: today's body STRICTLY swallows yesterday's - opens beyond
        # the prior close and closes beyond the prior open. Non-strict bounds
        # made every no-gap reversal an "engulfing" (an open equal to the
        # prior close satisfied <=), which turned a rare signal into noise.
        mark(np.concatenate(([False], up[cur] & prev_dn & (o[cur] < pc) & (c[cur] > po))),
             "bullish engulfing", "bullish")
        mark(np.concatenate(([False], dn[cur] & prev_up & (o[cur] > pc) & (c[cur] < po))),
             "bearish engulfing", "bearish")
        # harami: today's body strictly inside yesterday's, opposite colours
        mark(np.concatenate(([False], up[cur] & prev_dn & (o[cur] > pc) & (c[cur] < po))),
             "bullish harami", "bullish")
        mark(np.concatenate(([False], dn[cur] & prev_up & (o[cur] < pc) & (c[cur] > po))),
             "bearish harami", "bearish")
        # piercing / dark cloud: close beyond the midpoint of yesterday's body
        mid = (po + pc) / 2.0
        mark(np.concatenate(([False], up[cur] & prev_dn & (o[cur] < pc) & (c[cur] > mid) & (c[cur] < po))),
             "piercing line", "bullish")
        mark(np.concatenate(([False], dn[cur] & prev_up & (o[cur] > pc) & (c[cur] < mid) & (c[cur] > po))),
             "dark cloud cover", "bearish")

    if n >= 3:
        b0, b1, b2 = body[:-2], body[1:-1], body[2:]
        up0, up2 = up[:-2], up[2:]
        dn0, dn2 = dn[:-2], dn[2:]
        small_mid = b1 <= STAR_GAP_BODY * (b0 + b2) / 2.0
        c0, c2, o0, o2 = c[:-2], c[2:], o[:-2], o[2:]
        pad = [False, False]
        # morning star: big down, small pause, big up closing into candle 0's body
        mark(np.concatenate((pad, dn0 & small_mid & up2 & (c2 > (o0 + c0) / 2.0))),
             "morning star", "bullish")
        mark(np.concatenate((pad, up0 & small_mid & dn2 & (c2 < (o0 + c0) / 2.0))),
             "evening star", "bearish")
        # three soldiers / crows: three full-bodied candles marching one way
        strong = body / safe_rng >= 0.6
        s0, s1, s2 = strong[:-2], strong[1:-1], strong[2:]
        mark(np.concatenate((pad, up[:-2] & up[1:-1] & up[2:] & s0 & s1 & s2
                             & (c[1:-1] > c[:-2]) & (c[2:] > c[1:-1]))),
             "three white soldiers", "bullish")
        mark(np.concatenate((pad, dn[:-2] & dn[1:-1] & dn[2:] & s0 & s1 & s2
                             & (c[1:-1] < c[:-2]) & (c[2:] < c[1:-1]))),
             "three black crows", "bearish")

    if not hits:
        return pd.DataFrame(columns=["pattern", "direction"])
    idx = df.index
    out = pd.DataFrame(
        [{"date": idx[i], "pattern": p, "direction": d} for i, p, d in hits]
    ).drop_duplicates(subset=["date", "pattern"]).set_index("date").sort_index()
    return out


def pattern_record(df: pd.DataFrame, detections: pd.DataFrame,
                   pattern: str) -> dict:
    """The archive's verdict on one pattern: forward returns vs any day."""
    c = np.log(df["close"].to_numpy(dtype=float))
    pos_of = {ts: i for i, ts in enumerate(df.index)}
    days = [pos_of[d] for d in detections[detections["pattern"] == pattern].index
            if d in pos_of]
    out = {"pattern": pattern, "n": len(days), "horizons": {}}
    for hz in HORIZONS:
        fwd = np.array([c[i + hz] - c[i] for i in days if i + hz < len(c)])
        base = c[hz:] - c[:-hz]
        if not len(fwd):
            out["horizons"][str(hz)] = None
            continue
        out["horizons"][str(hz)] = {
            "mean_pct": float(fwd.mean()) * 100,
            "hit_rate": float((fwd > 0).mean()),
            "n": int(len(fwd)),
            "baseline_pct": float(base.mean()) * 100,
        }
    return out


def analyze_candles(df: pd.DataFrame, recent_days: int = 3) -> dict:
    """Recent detections plus each pattern's full-archive record."""
    det = detect_all(df)
    recent = det[det.index >= df.index[-1] - pd.Timedelta(days=recent_days)] \
        if len(det) else det
    rows = []
    for d, r in recent.iterrows():
        rec = pattern_record(df, det, r["pattern"])
        rows.append({"date": pd.Timestamp(d).date().isoformat(),
                     "pattern": r["pattern"], "direction": r["direction"],
                     "record": rec})
    return {
        "recent": rows,
        "note": ("most sessions print no defined pattern, and that is the "
                 "honest normal - the record beside each hit is the archive's "
                 "own verdict, overlapping windows stated in the module doc"),
    }
