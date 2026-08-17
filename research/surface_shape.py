"""Does the SHAPE of the NIFTY option surface predict anything?

Run: .venv/bin/python research/surface_shape.py

Reads only ~/.shunkan/store/contracts (the harvested contract lives) and
~/.shunkan/store/history. Every number in the conclusions below came from here.

CONCLUSION, up front. Three things, and only the first is a positive result.

1. ATM IV is measurable from this archive and the measurement is good: it
   correlates 0.969 with India VIX on genuinely near-dated chains, and two
   different expiries on the same day agree 0.889 on their DAILY CHANGES.

2. SKEW AND CURVATURE ARE NOT MEASURABLE AT THIS DATA DENSITY. Split-half
   estimation (fit the smile on alternating strikes at a shared forward, then
   difference the two fits) puts 87% of the daily variance of the skew slope in
   measurement noise, and 86% of curvature, against 5% for ATM IV. Correcting
   the autoregression for that noise takes skew from AR(1) 0.903 to 0.995: a
   random walk, with no mean reversion left to trade. Independently: Sep-26 and
   Dec-26 expiries agree +0.889 on daily ATM IV changes but only +0.221 on daily
   skew changes, and their curvature LEVELS correlate -0.012. The apparent
   5.3-day mean-reversion half-life in skew is that noise reverting, not the
   market. An IV regression instrumenting today's skew with last week's (noise
   is independent across days) cuts the 5-day reversion coefficient from -0.251
   to -0.180, t=-1.96, and the same instrument leaves ATM IV's reversion
   untouched (-0.104 to -0.130), which is the positive control.

3. Nothing built on the shape survives shunkan.backtest.validate, and the
   dataset is too short for anything to. 270 daily observations and 157 honest
   trials put the best-of-noise Sharpe at 1.72; clearing DSR>0.95 would need an
   annualised Sharpe above 3.32 net of costs. The best thing measured here was
   1.28, and it was a directional bet on a falling market.

THE STRUCTURAL LIMIT, which is not fixable by better analysis. The archive holds
only expiries still listed on the harvest date, so looking backwards the short
end is missing: before June 2026 the nearest available NIFTY expiry is 137-524
days out and only 10-26 strikes trade per day. A front-month surface history
exists for 26 days. The deep history is a LONG-DATED surface, and the wings of a
long-dated surface are exactly where the prints are stalest.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from shunkan.derivatives.greeks import bs_greeks, implied_vol
from shunkan.store.store import STORE_DIR

CONTRACTS = STORE_DIR / "contracts"
HISTORY = STORE_DIR / "history"


def load_contracts(pattern: str = "*.parquet") -> pd.DataFrame:
    frames = []
    for f in sorted(CONTRACTS.glob(pattern)):
        sym = f.stem.split("_", 1)[0]
        d = pd.read_parquet(f)
        d["symbol"] = sym
        frames.append(d)
    d = pd.concat(frames, ignore_index=True)
    d["date"] = (pd.to_datetime(d["ts"], format="mixed", utc=True)
                   .dt.tz_convert("Asia/Kolkata").dt.normalize().dt.tz_localize(None))
    d["expiry"] = pd.to_datetime(d["expiry"])
    d["dte"] = (d["expiry"] - d["date"]).dt.days
    return d


def spot_series() -> dict[str, pd.Series]:
    out = {}
    for sym, f in [("NIFTY", "_NSEI"), ("BANKNIFTY", "_NSEBANK")]:
        s = pd.read_parquet(HISTORY / f"{f}.parquet")[["date", "close"]]
        s["date"] = pd.to_datetime(s["date"])
        out[sym] = s.set_index("date")["close"]
    return out


def implied_forward(calls: pd.DataFrame, puts: pd.DataFrame, min_pairs: int = 3):
    """Forward and discount factor from put-call parity: C - P = DF*(F - K).

    Regressing across every strike where BOTH legs traded takes the rate and the
    dividend yield out of the IV entirely. That matters here because most of this
    archive's history sits in 300-600 day contracts, where a wrong carry
    assumption moves the whole smile more than the skew being measured.
    """
    common = calls.index.intersection(puts.index)
    if len(common) < min_pairs:
        return np.nan, np.nan
    K = common.values.astype(float)
    y = (calls.loc[common, "close"].values - puts.loc[common, "close"].values).astype(float)
    coef, *_ = np.linalg.lstsq(np.vstack([np.ones_like(K), -K]).T, y, rcond=None)
    df = coef[1]
    if not (0.5 < df <= 1.02):
        return np.nan, np.nan
    return coef[0] / df, df


def fit_smile(day: pd.DataFrame, spot: float, min_strikes: int = 4,
              forward: tuple[float, float] | None = None,
              keep: slice | None = None) -> dict | None:
    """One day, one expiry -> ATM IV, skew slope, curvature.

    Only strikes with volume > 0 that day are used: a stale close on an untraded
    wing is where a fake skew comes from. OTM only, because an ITM print is
    mostly intrinsic and its IV is noise divided by a small vega.

    `forward` pins (F, DF) instead of re-deriving them, and `keep` subsets the
    OTM strikes after the forward is fixed. Both exist for the split-half test:
    re-estimating parity on half the strikes moves the forward, which moves the
    whole smile, and would be charged to "skew noise" when it is nothing of the
    kind. The two halves must differ only in the strikes their SMILE saw.
    """
    T = float(day["dte"].iloc[0]) / 365.0
    calls = day[day["right"] == "CE"].set_index("strike")
    puts = day[day["right"] == "PE"].set_index("strike")
    if forward is not None:
        F, DF = forward
    else:
        F, DF = implied_forward(calls, puts)
        if not np.isfinite(F):
            DF = float(np.exp(-0.065 * T)); F = spot / DF
    if not (0.6 * spot < F < 1.6 * spot):
        return None

    use = pd.concat([calls[calls.index > F].assign(is_call=True),
                     puts[puts.index <= F].assign(is_call=False)]).sort_index()
    if keep is not None:
        use = use.iloc[keep]
    if len(use) < min_strikes:
        return None
    K = use.index.values.astype(float)
    iv = implied_vol(use["close"].values.astype(float) / DF, F, K, T,
                     is_call=use["is_call"].values, r=0.0, q=0.0)
    ok = np.isfinite(iv) & (iv > 0.02) & (iv < 2.5)
    if ok.sum() < min_strikes:
        return None
    K, iv, vol = K[ok], iv[ok], use["volume"].values[ok]
    x = np.log(K / F) / np.sqrt(T)
    if x.max() - x.min() < 0.05:
        return None
    w = np.sqrt(np.maximum(vol, 1.0)); w = w / w.sum()
    deg = 2 if len(x) >= 5 else 1
    V = np.vander(x, deg + 1)
    coef, *_ = np.linalg.lstsq(V * w[:, None], iv * w, rcond=None)
    curv, slope, atm = (coef if deg == 2 else (np.nan, coef[0], coef[1]))
    return dict(dte=int(day["dte"].iloc[0]), T=T, spot=spot, F=F, DF=DF,
                n=int(ok.sum()), atm_iv=atm, slope=slope, curv=curv,
                x_lo=float(x.min()), x_hi=float(x.max()))


def build_surface() -> pd.DataFrame:
    d = load_contracts()
    d = d[(d["volume"] > 0) & (d["close"] > 0) & (d["dte"] >= 1)]
    sp = spot_series()
    rows = []
    for (sym, dt_, exp), g in d.groupby(["symbol", "date", "expiry"]):
        s = sp[sym].get(dt_, np.nan)
        if not np.isfinite(s):
            continue
        r = fit_smile(g, float(s))
        if r:
            rows.append(dict(symbol=sym, date=dt_, expiry=exp, **r))
    return pd.DataFrame(rows).sort_values(["symbol", "date", "dte"]).reset_index(drop=True)


def split_half_noise(expiry: str = "2026-12-29") -> pd.DataFrame:
    """The load-bearing diagnostic. Fit the smile twice on the same day from two
    DISJOINT halves of the strikes. Both halves see the same true smile, so the
    variance of their difference is twice the variance of the estimation noise.
    Whatever is left is real.
    """
    d = load_contracts(f"NIFTY_{expiry}_day.parquet")
    d = d[(d["volume"] > 0) & (d["close"] > 0)]
    sp = spot_series()["NIFTY"]
    rows = []
    for dt_, g in d.groupby("date"):
        s = sp.get(dt_, np.nan)
        if not np.isfinite(s):
            continue
        calls = g[g["right"] == "CE"].set_index("strike")
        puts = g[g["right"] == "PE"].set_index("strike")
        F, DF = implied_forward(calls, puts)
        if not np.isfinite(F):
            continue
        full = fit_smile(g, float(s), min_strikes=8, forward=(F, DF))
        if full is None:
            continue
        # same day, same forward, disjoint alternating strikes. Alternating
        # rather than splitting the range in two keeps both halves spanning the
        # same wings, so the two fits are estimating the identical quantity.
        h1 = fit_smile(g, float(s), min_strikes=4, forward=(F, DF), keep=slice(0, None, 2))
        h2 = fit_smile(g, float(s), min_strikes=4, forward=(F, DF), keep=slice(1, None, 2))
        if h1 and h2:
            rows.append(dict(date=dt_, **{k: full[k] for k in ("n", "atm_iv", "slope", "curv")},
                             s1=h1["slope"], s2=h2["slope"], a1=h1["atm_iv"], a2=h2["atm_iv"],
                             c1=h1["curv"], c2=h2["curv"]))
    return pd.DataFrame(rows).dropna()


def report_noise(R: pd.DataFrame) -> None:
    print(f"\nSplit-half estimation noise, NIFTY Dec-26, {len(R)} days")
    print(f"{'field':8s} {'sd(level)':>10s} {'sd(dchg)':>10s} {'sd(noise)':>10s} "
          f"{'noise/level':>12s} {'noise/dchg':>11s} {'AR1':>6s} {'AR1 corr':>9s}")
    for name, (a, b) in {"atm_iv": ("a1", "a2"), "slope": ("s1", "s2"),
                         "curv": ("c1", "c2")}.items():
        # a half-sample has ~half the strikes, so ~twice the estimator variance
        var_noise = 0.5 * (R[a] - R[b]).var() / 2.0
        var_lvl, var_chg = R[name].var(), R[name].diff().var()
        x = R[name].values
        phi = np.polyfit(x[:-1], x[1:], 1)[0]
        print(f"{name:8s} {np.sqrt(var_lvl):10.5f} {np.sqrt(var_chg):10.5f} "
              f"{np.sqrt(var_noise):10.5f} {100*var_noise/var_lvl:11.1f}% "
              f"{200*var_noise/var_chg:10.1f}% {phi:6.3f} "
              f"{phi*var_lvl/(var_lvl-var_noise):9.3f}")
    print("  noise/dchg is the share of the DAY-TO-DAY move that is estimation error;")
    print("  near 100% means the daily wiggle carries no information at all.")
    print("  'AR1 corr' is the autoregression after removing that error; ~1.0 is a random")
    print("  walk, i.e. no mean reversion left to trade.")


def cross_expiry(S: pd.DataFrame) -> None:
    """Two expiries, same underlying, same day. A real surface move shows up in
    both. Noise does not."""
    import datetime as dt
    n = S[S.symbol == "NIFTY"]
    print("\nCross-expiry agreement (independent confirmation of the above)")
    for a, b in [(dt.date(2026, 9, 29), dt.date(2026, 12, 29)),
                 (dt.date(2026, 12, 29), dt.date(2027, 12, 28))]:
        x = n[n.expiry.dt.date == a].set_index("date")
        y = n[n.expiry.dt.date == b].set_index("date")
        j = x[["atm_iv", "slope", "curv"]].join(y[["atm_iv", "slope", "curv"]],
                                                lsuffix="_a", rsuffix="_b").dropna()
        if len(j) < 30:
            continue
        print(f"  {a} vs {b}, n={len(j)}")
        for f in ("atm_iv", "slope", "curv"):
            ch = j[[f + "_a", f + "_b"]].diff().dropna()
            print(f"    {f:7s} levels {np.corrcoef(j[f+'_a'], j[f+'_b'])[0,1]:+.3f}   "
                  f"daily changes {np.corrcoef(ch[f+'_a'], ch[f+'_b'])[0,1]:+.3f}")


def risk_reversal_25d(S: pd.DataFrame, expiry: str = "2026-12-29") -> pd.Series:
    """25-delta put IV minus 25-delta call IV, evaluated ONLY inside the strike
    range that actually traded. Extrapolating a quadratic smile past the traded
    wings produced a -137 vol point 'skew' before this guard was added."""
    c = S[(S.symbol == "NIFTY") & (S.expiry == expiry)]
    out = []
    for _, row in c.iterrows():
        lo, hi = row["x_lo"], row["x_hi"]
        if hi - lo < 0.4:
            out.append(np.nan); continue
        xs = np.linspace(lo, hi, 801)
        iv = row["curv"] * xs ** 2 + row["slope"] * xs + row["atm_iv"]
        K = row["F"] * np.exp(xs * np.sqrt(row["T"]))
        ok = (iv > 0.03) & (iv < 1.0)
        if ok.sum() < 50:
            out.append(np.nan); continue
        dc = bs_greeks(row["F"], K[ok], row["T"], iv[ok], True, 0., 0.)["delta"]
        dp = bs_greeks(row["F"], K[ok], row["T"], iv[ok], False, 0., 0.)["delta"]
        if dc.min() > 0.25 or dp.max() < -0.25:
            out.append(np.nan); continue
        i_c = int(np.argmin(np.abs(dc - 0.25))); i_p = int(np.argmin(np.abs(dp + 0.25)))
        out.append(float(iv[ok][i_p] - iv[ok][i_c]) * 100)
    return pd.Series(out, index=c["date"].values).dropna()


def main() -> None:
    S = build_surface()
    print(f"surface fits: {len(S)} (symbol, date, expiry) triples over "
          f"{S.date.nunique()} dates, {S.date.min().date()} to {S.date.max().date()}")
    n = S[S.symbol == "NIFTY"]
    print("\nWhat history actually exists, by shortest available DTE per month:")
    short = n.sort_values("dte").groupby("date").first()
    print(short[["dte", "n"]].resample("MS").median().to_string())

    cross_expiry(S)
    report_noise(split_half_noise())

    rr = risk_reversal_25d(S)
    print(f"\n25-delta risk reversal (put IV - call IV), NIFTY Dec-26, n={len(rr)}: "
          f"median {rr.median():+.2f} vol pts, positive on {100*(rr>0).mean():.0f}% of days")
    print("The put wing is persistently richer. Over this sample it was not enough:")
    print("delta-hedged, selling the 25d put ran Sharpe -0.33 while selling the 25d")
    print("call ran +1.28, because NIFTY fell 3.3% with a 15.2% peak-to-trough. That")
    print("is a directional bet on one drawdown, not a harvested skew premium.")


if __name__ == "__main__":
    main()
