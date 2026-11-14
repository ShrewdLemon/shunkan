"""End-of-day vol measures from the IV panel (fo_iv): ATM IV, 25-delta skew, a
constant-maturity series, IV rank and percentile, and the variance premium.

Only out-of-the-money strikes with an IV feed these (an ITM print is mostly
intrinsic), and nothing is extrapolated past what traded: ATM IV needs traded
strikes either side of the forward, the innermost two less than one standard
deviation apart; a 25-delta IV needs traded strikes on both sides of 25 delta;
an N-day value needs live expiries on both sides of N days. IVs are decimals
(0.13 = 13%).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from shunkan.data import fo_iv
from shunkan.derivatives import black76

IV_LO, IV_HI = 0.01, 3.0
MIN_T = 3 / 365          # expiries closer than this are left out of N-day values
DELTA_GAP = 0.2          # widest delta bracket a 25-delta IV may span
FIT_STRIKES = 4          # traded strikes used either side of ATM or 25 delta
MIN_SHARE = 0.0003       # an expiry trading under this share of the day's busiest (beyond
                         # MIN_T) is left out of N-day values: its prints are stale
TRADING_DAYS = 252
_G = ["date", "symbol", "expiry"]
_D = ["date", "symbol"]


def _otm(panel: pd.DataFrame) -> pd.DataFrame:
    if "delta" not in panel.columns:
        panel = fo_iv.with_greeks(panel)
    iv = panel["iv"].to_numpy()
    ok = (panel["code"] == black76.OK).to_numpy() & (iv > IV_LO) & (iv < IV_HI)
    call = (panel["right"] == "CE").to_numpy()
    k, f = panel["strike"].to_numpy(), panel["forward"].to_numpy()
    otm = np.where(call, k > f, k <= f)
    cols = _G + ["strike", "right", "forward", "t", "iv", "delta", "contracts"]
    return panel.loc[ok & otm, cols].assign(x=np.log(k[ok & otm] / f[ok & otm]))


def _line(lo: pd.DataFrame, hi: pd.DataFrame, col: str, at: float) -> pd.DataFrame:
    """Contract-weighted straight line through the rows either side of `at`, read at `at`,
    for groups with rows on both sides; also the gap between the two innermost rows."""
    both = lo[_G].drop_duplicates().merge(hi[_G].drop_duplicates(), on=_G)
    pts = pd.concat([lo, hi], ignore_index=True).merge(both, on=_G)
    w = pts["contracts"].to_numpy().astype(float)
    x = pts[col].to_numpy() - at
    y = pts["iv"].to_numpy()
    sums = pd.DataFrame({"w": w, "wx": w * x, "wy": w * y, "wxx": w * x * x, "wxy": w * x * y})
    sums[_G] = pts[_G]
    g = sums.groupby(_G, sort=False).sum().reset_index()
    den = g["w"] * g["wxx"] - g["wx"] ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        line = (g["wxx"] * g["wy"] - g["wx"] * g["wxy"]) / den
    flat = ~(np.abs(den) > 1e-12 * g["w"] ** 2)
    g["value"] = np.where(flat, g["wy"] / g["w"], line)
    inner = lo.groupby(_G, sort=False).head(1)[_G + [col]].merge(
        hi.groupby(_G, sort=False).head(1)[_G + [col]], on=_G, suffixes=("_lo", "_hi"))
    inner["gap"] = (inner[col + "_hi"] - inner[col + "_lo"]).abs()
    return g[_G + ["value"]].merge(inner[_G + ["gap"]], on=_G)


def _delta_iv(side: pd.DataFrame, target: float) -> pd.DataFrame:
    """IV at `target` delta from the FIT_STRIKES strikes either side of it; delta falls
    with strike for calls and puts alike."""
    s = side.sort_values(_G + ["strike"], kind="stable")
    low = (s["delta"] > target if target < 0 else s["delta"] >= target).to_numpy()
    lo = s[low].sort_values(_G + ["strike"], ascending=[True] * 3 + [False], kind="stable")
    fit = _line(lo.groupby(_G, sort=False).head(FIT_STRIKES),
                s[~low].groupby(_G, sort=False).head(FIT_STRIKES), "delta", target)
    good = fit["gap"].to_numpy() <= DELTA_GAP
    return fit.loc[good, _G].assign(iv=fit["value"].to_numpy()[good])


def by_expiry(panel: pd.DataFrame) -> pd.DataFrame:
    """Per (date, symbol, expiry): t, forward, contracts, share, atm_iv, put25_iv, call25_iv
    and skew25 = put25 - call25.

    ATM and 25-delta IVs are contract-weighted lines through the FIT_STRIKES nearest
    traded strikes either side, so a one-lot stale print barely moves them. share is the
    expiry's traded contracts over the day's busiest expiry at least MIN_T away (an
    expiry-day session trades hundreds of times the rest).
    """
    head = panel.groupby(_G, sort=False).agg(t=("t", "first"), forward=("forward", "first"),
                                             contracts=("contracts", "sum")).reset_index()
    busiest = head["contracts"].where(head["t"] >= MIN_T).groupby(
        [head[c] for c in _D], sort=False).transform("max")
    head["share"] = head["contracts"] / busiest
    otm = _otm(panel)
    puts = otm[otm["right"] == "PE"].sort_values(_G + ["strike"], ascending=[True] * 3 + [False],
                                                 kind="stable")
    calls = otm[otm["right"] == "CE"].sort_values(_G + ["strike"], kind="stable")
    atm = _line(puts.groupby(_G, sort=False).head(FIT_STRIKES),
                calls.groupby(_G, sort=False).head(FIT_STRIKES), "x", 0.0)
    atm = atm.merge(head[_G + ["t"]], on=_G)
    narrow = atm["gap"].to_numpy() <= atm["value"].to_numpy() * np.sqrt(atm["t"].to_numpy())
    out = head.merge(
        atm.loc[narrow, _G].assign(atm_iv=atm["value"].to_numpy()[narrow]), on=_G, how="left")
    for name, side, target in (("put25_iv", puts, -0.25), ("call25_iv", calls, 0.25)):
        out = out.merge(_delta_iv(side, target).rename(columns={"iv": name}), on=_G, how="left")
    out["skew25"] = out["put25_iv"] - out["call25_iv"]
    return out.sort_values(_G, kind="stable", ignore_index=True)


def constant_maturity(expiries: pd.DataFrame, days: int = 30) -> pd.DataFrame:
    """Per (date, symbol): atm_iv and skew25 at `days` calendar days, interpolated between
    the expiries either side (ATM in total variance, skew linearly in time)."""
    target = days / 365
    e = expiries[(expiries["t"] >= MIN_T) & expiries["atm_iv"].notna()
                 & (expiries["share"] >= MIN_SHARE)]
    e = e.sort_values(_D + ["t"], kind="stable")
    lo = e[e["t"] <= target].groupby(_D, sort=False).tail(1)
    hi = e[e["t"] >= target].groupby(_D, sort=False).head(1)
    cols = ["expiry", "t", "atm_iv", "skew25"]
    m = lo[_D + cols].merge(hi[_D + cols], on=_D, suffixes=("_lo", "_hi"))
    t1, t2 = m["t_lo"].to_numpy(), m["t_hi"].to_numpy()
    same = t2 <= t1
    with np.errstate(divide="ignore", invalid="ignore"):
        w = np.where(same, 0.0, (target - t1) / (t2 - t1))
        var = m["atm_iv_lo"].to_numpy() ** 2 * t1
        var = var + w * (m["atm_iv_hi"].to_numpy() ** 2 * t2 - var)
        atm = np.sqrt(var / target)
    s1, s2 = m["skew25_lo"].to_numpy(), m["skew25_hi"].to_numpy()
    skew = s1 + w * (s2 - s1)
    return (m[_D].assign(atm_iv=atm, skew25=skew, expiry_lo=m["expiry_lo"], expiry_hi=m["expiry_hi"])
            .sort_values(_D, kind="stable", ignore_index=True))


def iv_rank(iv: pd.Series, window: int = TRADING_DAYS) -> pd.Series:
    """(today - window low) / (window high - window low), the window ending today; NaN until full."""
    lo = iv.rolling(window, min_periods=window).min()
    hi = iv.rolling(window, min_periods=window).max()
    return (iv - lo) / (hi - lo)


def iv_percentile(iv: pd.Series, window: int = TRADING_DAYS) -> pd.Series:
    """Share of the previous window - 1 sessions with a lower value than today; NaN until full."""
    return iv.rolling(window, min_periods=window).apply(lambda a: float((a[:-1] < a[-1]).mean()), raw=True)


def variance_premium(iv: pd.Series, close: pd.Series, horizon: int = 21) -> pd.DataFrame:
    """Implied vs realized variance per session; both series indexed by session date.

    realized_var_next uses the next `horizon` sessions, so it is known only
    `horizon` sessions later: a research measure, never a signal at that date.
    rv_trailing (the past `horizon` sessions) is known at the close.
    """
    frame = pd.concat({"iv": iv, "close": close}, axis=1).dropna(subset=["close"])
    r2 = np.log(frame["close"] / frame["close"].shift(1)) ** 2
    scale = TRADING_DAYS / horizon
    trailing = r2.rolling(horizon, min_periods=horizon).sum() * scale
    nxt = r2.rolling(horizon, min_periods=horizon).sum().shift(-horizon) * scale
    out = pd.DataFrame({"iv": frame["iv"], "rv_trailing": np.sqrt(trailing),
                        "implied_var": frame["iv"] ** 2, "realized_var_next": nxt})
    out["premium_var"] = out["implied_var"] - out["realized_var_next"]
    out["premium_vol"] = out["iv"] - np.sqrt(out["realized_var_next"])
    out["iv_minus_rv_trailing"] = out["iv"] - out["rv_trailing"]
    return out
