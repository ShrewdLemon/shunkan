"""Weekly NIFTY iron condor gated on the variance risk premium. Pre-registered.

Run: SHUNKAN_HOME=<scratch> python research/vrp_condor.py --audit | --run --out PATH

Registration: docs/superpowers/specs/2026-09-29-fo-bhav-vrp-design.md Part 2,
resolved by the addendum in 2026-11-04-lane-c-design.md Part 3 (items 1-13).
Written and tested on synthetic chains before any expired option price was
read. --audit prints counts only; --run is the one registered run.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from shunkan.backtest.options import (DailyChain, Leg, mark, nearest_traded, parity_forward,
                                      simulate, straddle_vol, take_and_stop, take_profit)
from shunkan.backtest.validate import cvar, deflated_sharpe, newey_west_t, sharpe
from shunkan.config import APP_DIR
from shunkan.data import fo_bhav

FIRST_WEEKLY = pd.Timestamp("2019-02-14")
STEP = 50.0
MAX_STEPS = 10

THETAS = (None, 0.0, 2.0, 4.0)
EXITS = ("E0", "E1", "E2")
CONFIGS = [(t, e) for t in THETAS for e in EXITS]       # this order is the tie-break order
SLIPPAGES = (0.25, 0.5, 1.0)
LOT = 65
CHARGE_LOTS = 1           # owner: spec Part 3 item 9
DSR_STD = "search"        # owner: "search" | 1.0 ; spec Part 3 item 12(2)
N_TRIALS = 15
SPLIT = pd.Timestamp("2024-11-20")
TUESDAY_FROM = pd.Timestamp("2025-09-01")
OOS = (pd.Timestamp("2022-01-01"), pd.Timestamp("2026-09-30"))

DECIDE_S = 0.5
NW_LAGS = 4
HAR_FROM = pd.Timestamp("2007-09-01")
HAR_START_BY = pd.Timestamp("2007-09-30")     # addendum item 5: the fit starts in 2007-09
ATM_TO_VIX = 0.927
LOAD_RANGE = ("2019-01-01", "2026-09-30")
OWNER_HOME = Path.home() / ".shunkan"
OWNER_STORE = OWNER_HOME / "store"
CACHE = APP_DIR / "research" / "nifty_opt.parquet"
DEFAULT_OUT = APP_DIR / "research" / "vrp_condor_run.json"
STRADDLE_OK = (None, "no_short", "no_wing", "no_credit")


def expiries(opts: pd.DataFrame) -> list[pd.Timestamp]:
    """Days on which some contract of that expiry is in that day's file."""
    d, e = opts["date"].dt.normalize(), opts["expiry"].dt.normalize()
    return [pd.Timestamp(x) for x in sorted(d[d == e].unique())]


def weeks(exps: list[pd.Timestamp]) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    return [(a, b) for a, b in zip(exps, exps[1:]) if a >= FIRST_WEEKLY]


def sessions_after(calendar: list[pd.Timestamp], a, b) -> list[pd.Timestamp]:
    """Sessions s with a < s <= b."""
    a, b = pd.Timestamp(a), pd.Timestamp(b)
    return sorted(s for s in calendar if a < s <= b)


def session_calendar(opt_days, spot_days) -> list[pd.Timestamp]:
    """Option-data dates plus spot dates, within the first to last option-data date."""
    o = {pd.Timestamp(d) for d in opt_days}
    if not o:
        return []
    lo, hi = min(o), max(o)
    return sorted(o | {pd.Timestamp(d) for d in spot_days if lo <= pd.Timestamp(d) <= hi})


def daily_variance(spot: pd.DataFrame) -> pd.Series:
    """Garman-Klass plus squared overnight return; rows that are not finite are dropped."""
    s = spot.sort_values("date")
    px = s[["open", "high", "low", "close"]].astype(float)
    px = px.where(px > 0)
    o, h, lo, c = (px[k].to_numpy() for k in ("open", "high", "low", "close"))
    prev = np.r_[np.nan, c[:-1]]
    with np.errstate(divide="ignore", invalid="ignore"):
        v = (0.5 * np.log(h / lo) ** 2 - (2 * math.log(2) - 1) * np.log(c / o) ** 2
             + np.log(o / prev) ** 2)
    out = pd.Series(v, index=pd.DatetimeIndex(s["date"]).normalize(), name="v")
    return out[np.isfinite(out)]


def har_forecast(v: pd.Series, entry: pd.Timestamp, h: int) -> float:
    """HAR-RV mean variance over the next h sessions, fit on targets ending by entry. NaN if unfit."""
    x = v.to_numpy(float)
    n = len(x)
    p = int(v.index.searchsorted(pd.Timestamp(entry), side="right")) - 1
    if p < 0 or h < 1:
        return math.nan
    r = pd.Series(x)
    X = np.column_stack([np.ones(n), x, r.rolling(5).mean(), r.rolling(22).mean()])
    y = r.rolling(h).mean().shift(-h).to_numpy()
    ok = (np.arange(n) + h <= p) & np.isfinite(X).all(axis=1) & np.isfinite(y)
    if ok.sum() < X.shape[1] or not np.isfinite(X[p]).all():
        return math.nan
    beta = np.linalg.lstsq(X[ok], y[ok], rcond=None)[0]
    return max(float(X[p] @ beta), 1e-10)


@dataclass
class Week:
    entry: pd.Timestamp
    expiry: pd.Timestamp
    h: int
    T: float
    F: float
    K_atm: float
    sigma: float
    sigma_hat: float
    legs: list[Leg] | None
    skip: str | None

    @property
    def vrp(self) -> float:
        return 100.0 * (self.sigma - self.sigma_hat)


def build_week(chain: DailyChain, entry, expiry, spot_close: float, h: int,
               sigma_hat: float) -> Week:
    """Condor at entry closes: shorts at F e^(-/+ sigma sqrt T), wings at 2 sigma sqrt T."""
    entry, expiry = pd.Timestamp(entry), pd.Timestamp(expiry)
    T = h / 252
    w = Week(entry, expiry, h, T, math.nan, math.nan, math.nan, sigma_hat, None, None)
    pf = parity_forward(chain, entry, spot_close)
    if pf is None:
        w.skip = "no_atm"
        return w
    F, K, C, P = pf
    w.F, w.K_atm = F, K
    w.sigma = straddle_vol(F, K, C + P, T)
    if not math.isfinite(w.sigma):
        w.skip = "no_vol"
        return w
    m = w.sigma * math.sqrt(T)

    def strike(target, right, direction, beyond=None):
        return nearest_traded(chain, entry, target, right, STEP, direction, MAX_STEPS, beyond)

    sp, sc = strike(F * math.exp(-m), "PE", -1), strike(F * math.exp(m), "CE", 1)
    if sp is None or sc is None:
        w.skip = "no_short"
        return w
    wp = strike(F * math.exp(-2 * m), "PE", -1, beyond=sp)
    wc = strike(F * math.exp(2 * m), "CE", 1, beyond=sc)
    if wp is None or wc is None:
        w.skip = "no_wing"
        return w
    legs = [Leg("PE", wp, 1), Leg("PE", sp, -1), Leg("CE", sc, -1), Leg("CE", wc, 1)]
    if not -mark(chain, entry, legs) > 0:
        w.skip = "no_credit"
        return w
    w.legs = legs
    return w


def label(config) -> str:
    t, e = config
    return f"{'none' if t is None else format(t, 'g')}/{e}"


def exit_rule(name):
    if name == "E0":
        return None
    if name == "E1":
        return take_profit(0.5)
    if name == "E2":
        return take_and_stop(0.5, 1.5)
    raise ValueError(f"unknown exit {name!r}")


def week_pnl(week, chain, exit_days, settle, config, s) -> float:
    """Net points per unit; 0.0 when the week is skipped or its VRP is not above theta."""
    theta, ex = config
    if week.skip is not None or (theta is not None and not week.vrp > theta):
        return 0.0
    return simulate(chain, week.legs, week.entry, week.expiry, exit_days, settle, s,
                    exit_rule(ex), lot=LOT * CHARGE_LOTS).net


def grid(weeks_data) -> pd.DataFrame:
    """Net points per week, indexed by expiry, for every (config index, slippage)."""
    cols = pd.MultiIndex.from_product([range(len(CONFIGS)), SLIPPAGES], names=["config", "s"])
    vals = [[week_pnl(w, ch, ex, st, cfg, s) for cfg in CONFIGS for s in SLIPPAGES]
            for w, ch, ex, st in weeks_data]
    idx = pd.DatetimeIndex([w.expiry for w, *_ in weeks_data], name="expiry")
    return pd.DataFrame(vals, index=idx, columns=cols, dtype=float)


def _year_rows(index: pd.DatetimeIndex, year: int) -> np.ndarray:
    lo = max(pd.Timestamp(year, 1, 1), OOS[0])
    hi = min(pd.Timestamp(year, 12, 31), OOS[1])
    return (index >= lo) & (index <= hi)


def walk_forward(g: pd.DataFrame, s=DECIDE_S) -> tuple[pd.Series, dict[int, int]]:
    """Anchored: each January picks the best Sharpe on every earlier week, then trades the year."""
    sel = {}
    for y in range(OOS[0].year, OOS[1].year + 1):
        train = g[(g.index < pd.Timestamp(y, 1, 1)) & (g.index >= FIRST_WEEKLY)]
        scores = [sharpe(train[(c, s)].to_numpy(), periods=52) for c in range(len(CONFIGS))]
        sel[y] = int(np.argmax(scores))
    return reprice(g, sel, s), sel


def reprice(g: pd.DataFrame, selections: dict[int, int], s) -> pd.Series:
    parts = [g.loc[_year_rows(g.index, y), (c, s)] for y, c in sorted(selections.items())]
    return (pd.concat(parts) if parts else pd.Series(dtype=float)).rename("net")


def oos_rows(g: pd.DataFrame) -> pd.DataFrame:
    return g[(g.index >= OOS[0]) & (g.index <= OOS[1])]


def verdict(series_05: pd.Series, series_10: pd.Series, g: pd.DataFrame) -> dict:
    """The four registered tests on the stitched out-of-sample series. All four must pass."""
    x = np.asarray(series_05, float)
    mean_05 = float(x.mean()) if len(x) else math.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        nw = newey_west_t(x, NW_LAGS) if len(x) > 1 else math.nan
    oos = oos_rows(g)
    searched = float(np.std([sharpe(oos[(c, DECIDE_S)].to_numpy(), 52)
                             for c in range(len(CONFIGS))], ddof=1))
    chosen = searched if DSR_STD == "search" else float(DSR_STD)
    std = chosen if math.isfinite(chosen) and chosen > 0 else 1.0
    try:
        dsr = deflated_sharpe(series_05, n_trials=N_TRIALS, trial_sharpe_std=std,
                              periods=52).deflated
        dsr_error = None if math.isfinite(dsr) else "denominator <= 0 (skew and kurtosis)"
    except ValueError as e:
        dsr, dsr_error = math.nan, str(e)
    mean_10 = float(pd.Series(series_10, dtype=float).mean())
    idx = series_05.index
    mean_pre = float(series_05[idx < SPLIT].mean())
    mean_post = float(series_05[idx >= SPLIT].mean())
    p1 = bool(mean_05 > 0 and nw >= 2.0)
    p2 = bool(dsr >= 0.95)
    p3 = bool(mean_10 > 0)
    p4 = bool(mean_pre > 0 and mean_post > 0)
    return {"mean_05": mean_05, "nw_t_05": float(nw), "pass1": p1,
            "dsr": float(dsr), "pass2": p2, "mean_10": mean_10, "pass3": p3,
            "mean_pre": mean_pre, "mean_post": mean_post, "pass4": p4,
            "passes": p1 and p2 and p3 and p4, "n_weeks": len(x),
            "selections": walk_forward(g, DECIDE_S)[1], "dsr_std_used": std,
            "dsr_std_mode": str(DSR_STD), "dsr_std_searched": searched, "dsr_error": dsr_error}


def max_drawdown(x) -> float:
    """Largest fall of cumulative points below their running peak; the start is a peak at 0."""
    c = np.r_[0.0, np.cumsum(np.asarray(x, float))]
    return float((np.maximum.accumulate(c) - c).max())


def stats(x) -> dict:
    x = np.asarray(x, float)
    if len(x) == 0:
        return {"n": 0}
    with np.errstate(divide="ignore", invalid="ignore"):
        t = newey_west_t(x, NW_LAGS) if len(x) > 1 else math.nan
    out = {"n": len(x), "mean": float(x.mean()), "nw_t": float(t), "sharpe": sharpe(x, 52),
           "cvar5": cvar(x, 0.05), "worst": float(x.min()), "max_dd": max_drawdown(x)}
    out.update({f"{k}_inr": out[k] * LOT for k in ("mean", "cvar5", "worst")})
    return out


def terciles(vix: pd.Series) -> tuple[np.ndarray, list[float] | None]:
    """low / mid / high on cut points over every week's entry VIX; "none" where missing."""
    v = vix.to_numpy(float)
    fin = np.isfinite(v)
    if not fin.any():
        return np.full(len(v), "none", dtype=object), None
    c1, c2 = np.quantile(v[fin], [1 / 3, 2 / 3])
    lab = np.where(v <= c1, "low", np.where(v <= c2, "mid", "high")).astype(object)
    lab[~fin] = "none"
    return lab, [float(c1), float(c2)]


def breakdowns(g: pd.DataFrame, vix: pd.Series, s=DECIDE_S) -> dict:
    """Every config on the full sample, by entry-VIX tercile and by expiry-weekday era."""
    lab, cuts = terciles(vix.reindex(g.index))
    era = np.where(g.index < TUESDAY_FROM, "thursday", "tuesday")
    out = {}
    for c, cfg in enumerate(CONFIGS):
        x = g[(c, s)].to_numpy(float)
        out[label(cfg)] = {"full": stats(x),
                           "vix": {t: stats(x[lab == t]) for t in ("low", "mid", "high", "none")},
                           "era": {e: stats(x[era == e]) for e in ("thursday", "tuesday")}}
    return {"vix_cuts": cuts, "configs": out}


def _straddle_legs(week, chain, settle) -> list[Leg] | None:
    if (chain is None or week.skip not in STRADDLE_OK or not math.isfinite(week.K_atm)
            or not math.isfinite(settle)):
        return None
    return [Leg("CE", week.K_atm, -1), Leg("PE", week.K_atm, -1)]


def straddle_pnl(week, chain, exit_days, settle, s) -> float:
    """Short ATM straddle at entry closes held to settlement; 0.0 where it cannot be priced."""
    legs = _straddle_legs(week, chain, settle)
    if legs is None:
        return 0.0
    return simulate(chain, legs, week.entry, week.expiry, exit_days, settle, s, None,
                    lot=LOT * CHARGE_LOTS).net


# --- assembly from frames ---------------------------------------------------

def _dates(df: pd.DataFrame) -> pd.DataFrame:
    d = pd.to_datetime(df["date"])
    if d.dt.tz is not None:
        d = d.dt.tz_localize(None)
    return df.assign(date=d.astype("datetime64[ns]").dt.normalize()).sort_values(
        "date", kind="stable")


def _at(s: pd.Series, day) -> float:
    x = s.get(day)
    return math.nan if x is None else float(x)


def assemble(opts: pd.DataFrame, spot: pd.DataFrame, vix: pd.DataFrame | None = None) -> dict:
    """Every registered week as (Week, DailyChain, exit_days, settle), entry VIX, and counts."""
    if "symbol" in opts.columns:
        opts = opts[(opts["symbol"] == "NIFTY") & (opts["kind"] == "OPTIDX")]
    opts = opts.assign(date=opts["date"].dt.normalize(), expiry=opts["expiry"].dt.normalize())
    spot = _dates(spot)
    n_dupes = int(spot["date"].duplicated().sum())
    spot = spot.drop_duplicates("date", keep="last")
    opt_days = opts["date"].unique()
    calendar = session_calendar(opt_days, spot["date"])
    exps = expiries(opts)
    wk = weeks(exps)
    ent = pd.to_datetime(opts["expiry"].map({b: a for a, b in wk}))
    keep = ent.notna() & (opts["date"] >= ent) & (opts["date"] <= opts["expiry"])
    sub = opts.loc[keep, ["date", "expiry", "strike", "right", "close", "contracts"]]
    by_expiry = dict(list(sub.groupby("expiry")))
    empty = sub.iloc[:0]

    close = spot.set_index("date")["close"].astype(float)
    v_all = daily_variance(spot)
    v = v_all[v_all.index >= HAR_FROM]
    vixc = pd.Series(dtype=float)
    if vix is not None:
        vixc = _dates(vix).drop_duplicates("date", keep="last").set_index("date")["close"]

    n = dict.fromkeys(("no_entry_spot", "no_settle_close", "no_entry_vix", "har_nan",
                       "exit_marks", "exit_marks_none"), 0)
    skips, msgs, hs = Counter(), Counter(), Counter()
    rows, vix_at = [], {}
    for a, b in wk:
        sess = sessions_after(calendar, a, b)
        h, exit_days = len(sess), [d for d in sess if d < b]
        spot_a, settle = _at(close, a), _at(close, b)
        fc = har_forecast(v, a, h)
        sig_hat = math.sqrt(252 * fc) if math.isfinite(fc) else math.nan
        blank = Week(a, b, h, h / 252, math.nan, math.nan, math.nan, sig_hat, None, None)
        chain = None
        try:
            chain = DailyChain(by_expiry.get(b, empty), b)
        except ValueError as e:
            week, blank.skip = blank, f"chain: {e}"
        if chain is not None:
            if math.isfinite(spot_a):
                week = build_week(chain, a, b, spot_a, h, sig_hat)
            else:
                week, blank.skip = blank, "no_spot"
        if week.skip is None and not math.isfinite(settle):
            week.skip = "no_settle"
        if week.skip is None:
            try:
                week_pnl(week, chain, exit_days, settle, CONFIGS[0], DECIDE_S)
            except ValueError as e:
                week.skip = f"simulate: {e}"
        if week.skip is None:
            marks = [mark(chain, d, week.legs) for d in exit_days]
            n["exit_marks"] += len(marks)
            n["exit_marks_none"] += sum(m is None for m in marks)
        skips["ok" if week.skip is None else week.skip.split(":")[0]] += 1
        if week.skip and ":" in week.skip:
            msgs[week.skip] += 1
        hs[h] += 1
        vix_at[b] = _at(vixc, a)
        n["no_entry_spot"] += not math.isfinite(spot_a)
        n["no_settle_close"] += not math.isfinite(settle)
        n["no_entry_vix"] += not math.isfinite(vix_at[b])
        n["har_nan"] += not math.isfinite(fc)
        rows.append((week, chain, exit_days, settle))

    counts = {"opts_rows": len(opts), "sessions": len(calendar),
              "spot_sessions_added": len(calendar) - len(opt_days),
              "first_session": calendar[0] if calendar else None,
              "last_session": calendar[-1] if calendar else None,
              "expiries": len(exps), "weeks": len(wk),
              "first_expiry": wk[0][1] if wk else None, "last_expiry": wk[-1][1] if wk else None,
              "oos_weeks": sum(OOS[0] <= b <= OOS[1] for _, b in wk),
              "skips": dict(skips), "skip_messages": dict(msgs), "h": dict(sorted(hs.items())),
              "h_lt3": sum(c for k, c in hs.items() if k < 3),
              "h_gt6": sum(c for k, c in hs.items() if k > 6),
              "spot_rows": len(spot), "spot_dupes": n_dupes,
              "spot_first": spot["date"].iloc[0] if len(spot) else None,
              "spot_last": spot["date"].iloc[-1] if len(spot) else None,
              "har_first": v.index[0] if len(v) else None,
              "vix_first": vixc.index[0] if len(vixc) else None,
              "vix_last": vixc.index[-1] if len(vixc) else None,
              "spot_var_dropped": max(len(spot) - 1 - len(v_all), 0), **n}
    return {"rows": rows, "vix": pd.Series(vix_at, dtype=float), "counts": counts}


def _d(x) -> str:
    return "-" if x is None else f"{x:%Y-%m-%d}"


def audit_lines(prep: dict) -> list[str]:
    """Counts only: no prices, P&L, sigma or VRP, and no statistic of them."""
    n = prep["counts"]
    out = [f"option rows {n['opts_rows']}, sessions {n['sessions']} "
           f"({_d(n['first_session'])} to {_d(n['last_session'])})",
           f"sessions added from spot calendar {n['spot_sessions_added']}",
           f"expiries {n['expiries']}, weeks {n['weeks']} (expiring {_d(n['first_expiry'])} to "
           f"{_d(n['last_expiry'])}), out-of-sample weeks {n['oos_weeks']}",
           "weeks by outcome: " + ", ".join(f"{k} {c}" for k, c in sorted(n["skips"].items()))]
    out += [f"  {c} x {m}" for m, c in sorted(n["skip_messages"].items())]
    out += ["sessions per week: " + ", ".join(f"{h}: {c}" for h, c in n["h"].items()),
            f"weeks with fewer than 3 sessions {n['h_lt3']}, more than 6 {n['h_gt6']}",
            f"weeks whose expiry lacks a NIFTY close {n['no_settle_close']}",
            f"weeks whose entry lacks a NIFTY close {n['no_entry_spot']}",
            f"weeks whose entry lacks a VIX close {n['no_entry_vix']}",
            f"weeks without a HAR forecast {n['har_nan']}",
            f"exit-day marks with an untraded leg {n['exit_marks_none']} of {n['exit_marks']}",
            f"spot rows {n['spot_rows']}, duplicate dates {n['spot_dupes']}, "
            f"variance rows dropped {n['spot_var_dropped']}",
            f"spot dates {_d(n['spot_first'])} to {_d(n['spot_last'])}, first HAR row "
            f"{_d(n['har_first'])}, VIX dates {_d(n['vix_first'])} to {_d(n['vix_last'])}"]
    return out


def report(prep: dict) -> dict:
    """The registered run: grid, walk-forward, verdict and every reported breakdown."""
    rows, vix = prep["rows"], prep["vix"]
    g = grid(rows)
    s05, sel = walk_forward(g, DECIDE_S)
    s10, s025 = reprice(g, sel, 1.0), reprice(g, sel, 0.25)
    v = verdict(s05, s10, g)
    oos = oos_rows(g)
    strad = pd.Series([straddle_pnl(w, ch, ex, st, DECIDE_S) for w, ch, ex, st in rows],
                      index=g.index, dtype=float)
    sig = np.array([w.sigma for w, *_ in rows], float)
    vx = vix.reindex(g.index).to_numpy(float)
    ok = np.isfinite(sig) & np.isfinite(vx) & (vx > 0)
    ratio = sig[ok] / (vx[ok] / 100)
    weeks_out = [{"entry": w.entry, "expiry": w.expiry, "h": w.h, "skip": w.skip, "F": w.F,
                  "K_atm": w.K_atm, "sigma": w.sigma, "sigma_hat": w.sigma_hat, "vrp": w.vrp,
                  "vix": x, "settle": st, "legs": w.legs}
                 for (w, _, _, st), x in zip(rows, vx)]
    return {
        "verdict": v,
        "selections": {y: label(CONFIGS[c]) for y, c in sel.items()},
        "selected": {"s0.5": stats(s05), "s1.0": stats(s10), "s0.25": stats(s025)},
        "configs": {label(cfg): {s: stats(oos[(c, s)]) for s in SLIPPAGES}
                    for c, cfg in enumerate(CONFIGS)},
        "breakdowns": breakdowns(g, vix, DECIDE_S),
        "straddle": {"s": DECIDE_S, "stats": stats(strad),
                     "n_priced": sum(_straddle_legs(w, ch, st) is not None
                                     for w, ch, _, st in rows),
                     "sigma_over_vix": {"n": int(ok.sum()),
                                        "mean": float(ratio.mean()) if ok.any() else math.nan,
                                        "sd": float(ratio.std(ddof=1)) if ok.sum() > 1
                                        else math.nan,
                                        "reference": ATM_TO_VIX}},
        "constants": {"LOT": LOT, "CHARGE_LOTS": CHARGE_LOTS, "DSR_STD": DSR_STD,
                      "N_TRIALS": N_TRIALS, "SLIPPAGES": SLIPPAGES,
                      "CONFIGS": [label(c) for c in CONFIGS], "SPLIT": SPLIT,
                      "TUESDAY_FROM": TUESDAY_FROM, "OOS": OOS, "FIRST_WEEKLY": FIRST_WEEKLY,
                      "HAR_FROM": HAR_FROM, "HAR_START_BY": HAR_START_BY,
                      "NW_LAGS": NW_LAGS},
        "counts": prep["counts"],
        "weeks": weeks_out,
        "series": {"selected_0.5": s05, "selected_1.0": s10, "selected_0.25": s025,
                   "straddle_0.5": strad},
        "grid": {"index": list(g.index), "columns": [list(c) for c in g.columns],
                 "values": g.to_numpy().tolist()},
    }


def _key(k) -> str:
    return f"{k:%Y-%m-%d}" if isinstance(k, pd.Timestamp) else str(k)


def clean(o):
    """JSON-safe copy: dates as YYYY-MM-DD, non-finite floats as null."""
    if isinstance(o, dict):
        return {_key(k): clean(v) for k, v in o.items()}
    if isinstance(o, pd.Series):
        return {_key(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, pd.Timestamp):
        return f"{o:%Y-%m-%d}"
    if isinstance(o, Leg):
        return [o.right, o.strike, o.qty]
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return float(o) if math.isfinite(o) else None
    return o


def _f(x, spec="+.2f") -> str:
    return "nan" if x is None or not math.isfinite(x) else format(x, spec)


def _row(name: str, st: dict) -> str:
    if not st.get("n"):
        return f"{name:<16} {0:>4}"
    return (f"{name:<16} {st['n']:>4} {_f(st['mean']):>8} {_f(st['mean_inr'], '+.0f'):>8} "
            f"{_f(st['nw_t']):>7} {_f(st['sharpe']):>7} {_f(st['cvar5']):>8} "
            f"{_f(st['cvar5_inr'], '+.0f'):>8} {_f(st['worst']):>8} "
            f"{_f(st['worst_inr'], '+.0f'):>8} {_f(st['max_dd'], '.1f'):>8}")


def report_lines(rep: dict) -> list[str]:
    v, ok = rep["verdict"], (lambda b: "pass" if b else "FAIL")
    head = f"{'':<16} {'n':>4} {'mean':>8} {'mean Rs':>8} {'NW t':>7} {'Sharpe':>7} " \
           f"{'CVaR5':>8} {'CVaR5 Rs':>8} {'worst':>8} {'worst Rs':>8} {'max DD':>8}"
    out = [f"VERDICT: {'PASS' if v['passes'] else 'FAIL'}  ({v['n_weeks']} out-of-sample weeks; "
           f"net points per unit, Rs at today's lot of {LOT}; the verdict is in points)",
           f"  (1) mean at s=0.5 {_f(v['mean_05'])} (Rs {_f(v['mean_05'] * LOT, '+.0f')}), "
           f"NW t {_f(v['nw_t_05'])} (>= 2.0): {ok(v['pass1'])}",
           f"  (2) DSR {_f(v['dsr'], '.3f')} with {N_TRIALS} trials, trial Sharpe sd "
           f"{_f(v['dsr_std_used'], '.3f')} [{v['dsr_std_mode']}; searched "
           f"{_f(v['dsr_std_searched'], '.3f')}] (>= 0.95): {ok(v['pass2'])}",
           *([f"      DSR undefined: {v['dsr_error']}"] if v.get("dsr_error") else []),
           f"  (3) same path at s=1.0, mean {_f(v['mean_10'])} "
           f"(Rs {_f(v['mean_10'] * LOT, '+.0f')}): {ok(v['pass3'])}",
           f"  (4) mean before {SPLIT:%Y-%m-%d} {_f(v['mean_pre'])}, from it "
           f"{_f(v['mean_post'])}: {ok(v['pass4'])}",
           "selections: " + ", ".join(f"{y} {c}" for y, c in rep["selections"].items()),
           "", "selected path, out-of-sample", head]
    out += [_row(f"s={s}", rep["selected"][f"s{s}"]) for s in (0.25, 0.5, 1.0)]
    out += ["", "every config, out-of-sample, s=0.5", head]
    out += [_row(k, c[DECIDE_S]) for k, c in rep["configs"].items()]
    b = rep["breakdowns"]
    cuts = "n/a" if b["vix_cuts"] is None else " / ".join(_f(c, ".2f") for c in b["vix_cuts"])
    out += ["", f"full sample, s=0.5, mean (n): VIX terciles cut at {cuts}; eras split "
                f"{TUESDAY_FROM:%Y-%m-%d}",
            f"{'':<16} {'low':>12} {'mid':>12} {'high':>12} {'no VIX':>12} {'thursday':>12} "
            f"{'tuesday':>12}"]
    for k, c in b["configs"].items():
        cells = [c["vix"][t] for t in ("low", "mid", "high", "none")] + \
                [c["era"][e] for e in ("thursday", "tuesday")]
        out.append(f"{k:<16} " + " ".join(
            f"{_f(x.get('mean', math.nan)) + ' (' + str(x['n']) + ')':>12}" for x in cells))
    out += ["", "tail risk by config, full sample, s=0.5", head]
    out += [_row(k, c["full"]) for k, c in b["configs"].items()]
    st, r = rep["straddle"], rep["straddle"]["sigma_over_vix"]
    out += ["", f"short ATM straddle held to settlement, full sample, s={st['s']} "
                f"({st['n_priced']} weeks priced)", head, _row("straddle", st["stats"]),
            f"sigma_ATM / VIX: mean {_f(r['mean'], '.3f')}, sd {_f(r['sd'], '.3f')}, "
            f"n {r['n']} (reference {r['reference']})"]
    return out


# --- data (main only) -------------------------------------------------------

def _outside_owner_home(path) -> Path:
    p = Path(os.path.abspath(path))
    if p.is_relative_to(Path(os.path.abspath(OWNER_HOME))):
        raise RuntimeError(f"{p} is under {OWNER_HOME}: set SHUNKAN_HOME to a scratch home")
    return p


def load_options(refresh: bool = False) -> pd.DataFrame:
    """NIFTY index options over LOAD_RANGE, cached under SHUNKAN_HOME; the owner store is only read."""
    cache = _outside_owner_home(CACHE)
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)
    opts = fo_bhav.load_range(*LOAD_RANGE, symbols=["NIFTY"], kinds=["OPTIDX"], root=OWNER_STORE)
    if opts.empty:
        raise RuntimeError(f"no NIFTY option rows under {OWNER_STORE}; nothing cached")
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    opts.to_parquet(tmp, index=False)
    tmp.replace(cache)
    return opts


def load_history(name: str) -> pd.DataFrame:
    """A daily history file from the owner's store, dates as datetime64[ns] midnight."""
    return _dates(pd.read_parquet(OWNER_STORE / "history" / f"{name}.parquet")).reset_index(
        drop=True)


def _commit() -> dict:
    root = Path(__file__).resolve().parents[1]

    def git(*args):
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    status = git("status", "--porcelain", "--untracked-files=no")
    return {"head": git("rev-parse", "HEAD"), "dirty": None if status is None else bool(status)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vrp_condor.py",
                                 description="Pre-registered weekly NIFTY iron condor, VRP-gated.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit", action="store_true",
                      help="coverage counts only: no prices, P&L, sigma or VRP")
    mode.add_argument("--run", action="store_true",
                      help="the one registered run: grid, walk-forward, verdict, reports")
    ap.add_argument("--out", type=Path,
                    help="JSON written by --run (default $SHUNKAN_HOME/research/"
                         "vrp_condor_run.json); an existing file is never overwritten")
    ap.add_argument("--refresh-cache", action="store_true",
                    help="re-read the bhavcopies into the option cache")
    a = ap.parse_args(argv)
    out = a.out or DEFAULT_OUT
    if a.run and a.refresh_cache:
        ap.error("--run reads the cache the audit counted: refresh it with --audit first")
    if a.run and out.exists():
        ap.error(f"{out} exists: a rerun is reported next to the first run, never over it")
    commit = _commit()
    if a.run and (commit["head"] is None or commit["dirty"] is not False):
        ap.error(f"git HEAD {commit['head']}, tracked changes {commit['dirty']}: "
                 "the registered run must be at a committed hash")
    try:
        if a.run:
            _outside_owner_home(out)
        opts = load_options(a.refresh_cache)
    except RuntimeError as e:
        ap.error(str(e))
    prep = assemble(opts, load_history("NIFTY"), load_history("_INDIAVIX"))
    n = prep["counts"]
    if n["har_first"] is None or n["har_first"] > HAR_START_BY:
        ap.error(f"first HAR row {_d(n['har_first'])} is after {HAR_START_BY:%Y-%m-%d} "
                 f"(spot {_d(n['spot_first'])} to {_d(n['spot_last'])}): addendum item 5 "
                 "fits from 2007-09")
    if a.audit:
        print(f"commit {commit['head']}, tracked changes {commit['dirty']}")
        print("\n".join(audit_lines(prep)))
        return 0
    rep = report(prep)
    rep["commit"] = commit
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(clean(rep), indent=1, allow_nan=False))
    print("\n".join(audit_lines(prep) + [""] + report_lines(rep)))
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
