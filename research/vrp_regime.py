"""Is there a tradeable way to harvest the Indian variance risk premium?

Run: .venv/bin/python research/vrp_regime.py

Reads only ~/.shunkan/store/history, so it reproduces from the archive rather
than from a live session. Every number in the write-up below came from here.

CONCLUSION, stated up front so nobody has to infer it: the premium is real and
large, the naive trade does not survive the ATM-to-VIX haircut, and the
regime-gated version does not pass our own validators. We have a confirmed
phenomenon and no confirmed way to harvest it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from shunkan.backtest.validate import deflated_sharpe, permutation_test
from shunkan.store.store import STORE_DIR

# Measured, not assumed: ATM straddle IV runs below the index. 77 observations
# reconstructed from currently-listed contracts gave 0.927 mean, sd 0.051, with
# 98.7% below 1.0. You do not sell the VIX, you sell the straddle, and this
# haircut is what decides whether the trade pays.
ATM_TO_VIX = 0.927
HORIZON_D = 21
LOT = 65


def load() -> pd.DataFrame:
    h = STORE_DIR / "history"
    vix = pd.read_parquet(h / "_INDIAVIX.parquet")[["date", "close"]]
    nif = pd.read_parquet(h / "_NSEI.parquet")[["date", "close"]]
    d = (vix.rename(columns={"close": "vix"})
            .merge(nif.rename(columns={"close": "spot"}), on="date")
            .sort_values("date").reset_index(drop=True))
    d["date"] = pd.to_datetime(d["date"])
    r = np.log(d.spot).diff()
    d["rv21"] = r.rolling(HORIZON_D).std() * np.sqrt(252) * 100
    # Lagged: today's signal may only use vol realised up to yesterday.
    d["gap"] = d.vix - d.rv21.shift(1)
    # Brenner-Subrahmanyam ATM approximation, priced off the haircut IV.
    d["straddle_px"] = (0.7979 * d.spot * (d.vix * ATM_TO_VIX / 100)
                        * np.sqrt(HORIZON_D / 252))
    d["move"] = (d.spot.shift(-HORIZON_D) - d.spot).abs()
    d["pnl"] = d.straddle_px - d.move          # short straddle, index points
    # Expanding-window threshold: the tercile boundary may only be computed
    # from data that existed at the time, or the "out-of-sample" is a fiction.
    d["rich"] = d.gap > d.gap.expanding(500).quantile(2 / 3).shift(1)
    return d


def newey_west_t(x, lags: int = HORIZON_D) -> float:
    """Overlapping 21-day windows share returns, so a plain t-stat is inflated."""
    x = np.asarray(x, float)
    n = len(x)
    e = x - x.mean()
    v = (e @ e) / n
    for L in range(1, lags + 1):
        v += 2 * (1 - L / (lags + 1)) * ((e[L:] @ e[:-L]) / n)
    return float(x.mean() / np.sqrt(v / n))


def main() -> None:
    d = load()
    fwd = np.log(d.spot).diff().shift(-1).rolling(HORIZON_D).std().shift(-(HORIZON_D - 1))
    d["rv_fwd"] = fwd * np.sqrt(252) * 100
    v = d.dropna(subset=["rv_fwd"])
    nl_v = v.iloc[::HORIZON_D]
    print("1. THE PREMIUM ITSELF (VIX minus subsequent realised)")
    print(f"   overlapping n={len(v):,}  mean {(v.vix - v.rv_fwd).mean():+.3f} vol pts"
          f"  positive {100 * ((v.vix - v.rv_fwd) > 0).mean():.1f}%")
    print(f"   non-overlapping n={len(nl_v)}  t={newey_west_t((nl_v.vix - nl_v.rv_fwd).values, 1):.2f}")

    s = d.dropna(subset=["pnl", "rich"])
    print("\n2. SELLING IT, after the measured ATM haircut")
    for lbl, g in (("unconditional", s), ("rich tercile", s[s.rich]), ("rest", s[~s.rich])):
        print(f"   {lbl:16s} n={len(g):>5,}  {g.pnl.mean():+8.1f} pts  NW t={newey_west_t(g.pnl.values):+.2f}")

    print("\n3. OUR OWN GATE, non-overlapping windows")
    nl = s.iloc[::HORIZON_D].reset_index(drop=True)
    ret = nl.pnl / (nl.spot * 0.01)
    p = permutation_test(nl.rich.astype(float), ret, n_permutations=2000, block_size=3)
    # n_trials is deliberately generous to the strategy. The real search touched
    # the unconditional straddle, an afternoon decay window, box spreads,
    # condors, and this gate across several thresholds, lookbacks and ratios.
    dsr = deflated_sharpe(pd.Series(nl.rich.values * ret.values), n_trials=20)
    print(f"   permutation p={p.p_value:.4f}   deflated DSR={dsr.deflated:.3f}"
          f"  (obs {dsr.observed_sharpe:.2f} vs {dsr.expected_max_sharpe:.2f} from 20 trials)")
    print(f"   VERDICT: {'PASS' if (p.significant and dsr.survives) else 'REJECTED by our own gate'}")

    print("\n4. THE TAIL YOU ARE SELLING")
    rich = s[s.rich]
    print(f"   worst window {rich.pnl.min():,.0f} pts = Rs {rich.pnl.min() * LOT:,.0f}/lot"
          f"   skew {rich.pnl.skew():.2f}")

    cur = d.dropna(subset=["gap"]).iloc[-1]
    thr = d.gap.expanding(500).quantile(2 / 3).shift(1).iloc[-1]
    print(f"\n5. TODAY  VIX {cur.vix:.2f}  gap {cur.gap:+.2f} vs threshold {thr:+.2f}"
          f"  -> {'RICH' if cur.gap > thr else 'FLAT'}")


if __name__ == "__main__":
    main()
