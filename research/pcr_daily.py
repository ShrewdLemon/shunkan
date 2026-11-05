"""Daily NIFTY put-call ratio against next-open returns: 8 tests. Pre-registered.

Run: SHUNKAN_HOME=<scratch> python research/pcr_daily.py --audit | --run --out PATH

Registration: docs/superpowers/specs/2026-09-29-fo-bhav-vrp-design.md Part 3,
resolved by the addendum in 2026-11-04-lane-c-design.md Part 3 (items 14-18).
Written and tested on synthetic data before any option open interest was read.
--audit prints counts only; --run is the one registered run.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from shunkan.backtest.validate import holm, newey_west_t, t_pvalue
from shunkan.config import APP_DIR
from vrp_condor import _commit, _dates, _outside_owner_home, clean, load_history, load_options

DEFS = ("near", "all")
TRANSFORMS = ("z60", "d1")
HORIZONS = (1, 5)
WINDOW = 60
LEAD = max(HORIZONS) + 1          # r5 needs the open at t+6
CONTROLS = ["past1", "past5"]
SAMPLE_FROM = pd.Timestamp("2019-01-01")
BAR_T = 2.5
ALPHA = 0.05
DEFAULT_OUT = APP_DIR / "research" / "pcr_daily_run.json"


def _day(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s).astype("datetime64[ns]").dt.normalize()


def _options(opts: pd.DataFrame) -> pd.DataFrame:
    """NIFTY index option rows only; futures (right None) and other symbols are dropped."""
    if "symbol" in opts.columns:
        opts = opts[(opts["symbol"] == "NIFTY") & (opts["kind"] == "OPTIDX")]
    opts = opts[opts["right"].isin(("PE", "CE"))]
    return opts.assign(date=_day(opts["date"]), expiry=_day(opts["expiry"]))


def _ratio(rows: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.Series:
    g = rows.groupby(["date", "right"])["oi"].sum().unstack("right")
    g = g.reindex(index=dates, columns=["PE", "CE"]).astype(float)
    return g["PE"] / g["CE"].where(g["CE"] > 0)


def pcr_series(opts: pd.DataFrame) -> pd.DataFrame:
    """PE over CE open interest per session: the nearest expiry strictly after t, and every expiry."""
    o = _options(opts)
    dates = pd.DatetimeIndex(sorted(o["date"].unique()), name="date")
    later = o[o["expiry"] > o["date"]]
    near = later[later["expiry"] == later.groupby("date")["expiry"].transform("min")]
    return pd.DataFrame({"near": _ratio(near, dates), "all": _ratio(o, dates)}, index=dates)


def transforms(pcr: pd.DataFrame) -> pd.DataFrame:
    """z60 against the 60 rows before t (ddof 1) and the one-row change, by position."""
    out = {}
    for d in pcr.columns:
        x = pcr[d].astype(float)
        past = x.shift(1).rolling(WINDOW, min_periods=WINDOW)
        out[f"{d}_z60"] = (x - past.mean()) / past.std(ddof=1)
        out[f"{d}_d1"] = x - x.shift(1)
    return pd.DataFrame(out, index=pcr.index).replace([np.inf, -np.inf], np.nan)


def outcomes(spot: pd.DataFrame, dates) -> pd.DataFrame:
    """Next-open returns and trailing close returns on the sessions in both, by position."""
    s = _dates(spot).drop_duplicates("date", keep="last").set_index("date")
    want = pd.DatetimeIndex(dates).astype("datetime64[ns]").normalize()
    idx = pd.DatetimeIndex(s.index.intersection(want).unique().sort_values(), name="date")
    px = s.loc[idx, ["open", "close"]].astype(float)
    lp = np.log(px.where(px > 0))
    lo, lc = lp["open"], lp["close"]
    return pd.DataFrame({"r1": lo.shift(-2) - lo.shift(-1), "r5": lo.shift(-LEAD) - lo.shift(-1),
                         "past1": lc - lc.shift(1), "past5": lc - lc.shift(5)}, index=idx)


def _complete(signal, outcome, controls=None) -> pd.DataFrame:
    """Rows where signal, outcome and every control are finite, aligned on the index."""
    df = pd.DataFrame({"s": signal, "o": outcome})
    if controls is not None:
        c = pd.DataFrame(controls)
        df = df.join(c.set_axis([f"c{i}" for i in range(c.shape[1])], axis=1))
    return df.astype(float).replace([np.inf, -np.inf], np.nan).dropna()


def _z(x: np.ndarray, scale: float) -> np.ndarray | None:
    x = x - x.mean()
    sd = math.sqrt(x @ x / len(x))
    return x / sd if sd > 1e-9 * scale else None


def ic_test(signal, outcome, lags, controls=None) -> tuple[float, float]:
    """Spearman IC and the NW t of the standardised rank product; controls residualise both ranks."""
    df = _complete(signal, outcome, controls)
    n = len(df)
    if n < 3:
        return math.nan, math.nan
    r = df.rank(method="average").to_numpy(float)
    y = r[:, :2]
    if controls is not None:
        X = np.column_stack([np.ones(n), r[:, 2:]])
        y = y - X @ np.linalg.lstsq(X, y, rcond=None)[0]
    zs, zo = _z(y[:, 0], n), _z(y[:, 1], n)
    if zs is None or zo is None:
        return math.nan, math.nan
    m = zs * zo
    return float(m.mean()), newey_west_t(m, lags)


def run_tests(sig: pd.DataFrame, out: pd.DataFrame) -> pd.DataFrame:
    """The 8 registered tests, Holm-adjusted over the 8, separately for raw and partial."""
    ctl = out[CONTROLS]
    rows = {}
    for d in DEFS:
        for tr in TRANSFORMS:
            for h in HORIZONS:
                s, y = sig[f"{d}_{tr}"], out[f"r{h}"]
                ic, t = ic_test(s, y, h)
                icp, tp = ic_test(s, y, h, controls=ctl)
                rows[f"{d}_{tr}_r{h}"] = {
                    "def": d, "transform": tr, "h": h, "lags": h,
                    "n": len(_complete(s, y)), "n_partial": len(_complete(s, y, ctl)),
                    "ic": ic, "t": t, "p": t_pvalue(t),
                    "ic_partial": icp, "t_partial": tp, "p_partial": t_pvalue(tp)}
    res = pd.DataFrame.from_dict(rows, orient="index")
    res["p_holm"] = holm(res["p"])
    res["p_partial_holm"] = holm(res["p_partial"])
    res["survives"] = ((res["p_holm"] < ALPHA) & (res["t"].abs() >= BAR_T)
                       & (res["p_partial_holm"] < ALPHA) & (res["t_partial"].abs() >= BAR_T)
                       & (np.sign(res["t"]) == np.sign(res["t_partial"])))
    return res


def assemble(opts: pd.DataFrame, spot: pd.DataFrame) -> dict:
    """PCR on the joint index; signals and outcomes on the registered sample; audit counts."""
    used = _options(opts)
    pcr = pcr_series(used)
    out_all = outcomes(spot, pcr.index)
    idx = out_all.index
    pcr = pcr.reindex(idx)
    sig_all = transforms(pcr)
    ok = sig_all[[f"{d}_z60" for d in DEFS]].notna().all(axis=1).to_numpy() & (idx >= SAMPLE_FROM)
    start, end = (int(np.argmax(ok)) if ok.any() else len(idx)), max(len(idx) - LEAD, 0)
    sample = idx[start:end]
    sig, out = sig_all.loc[sample], out_all.loc[sample]
    spot_days = _dates(spot)["date"]
    inside = spot_days.drop_duplicates()
    inside = inside[inside.between(idx[0], idx[-1])] if len(idx) else inside.iloc[:0]
    counts = {"option_rows": len(used), "option_rows_ignored": len(opts) - len(used),
              "option_sessions": int(used["date"].nunique()),
              "spot_sessions": int(spot_days.nunique()),
              "spot_dupes": int(spot_days.duplicated().sum()),
              "joint_sessions": len(idx),
              "joint_first": idx[0] if len(idx) else None,
              "joint_last": idx[-1] if len(idx) else None,
              "option_sessions_without_spot": int(used["date"].nunique()) - len(idx),
              "spot_sessions_without_options": int((~inside.isin(idx)).sum()),
              "sample_sessions": len(sample),
              "sample_first": sample[0] if len(sample) else None,
              "sample_last": sample[-1] if len(sample) else None,
              "nan_pcr": {c: int(pcr.loc[sample, c].isna().sum()) for c in pcr.columns},
              "nan_signals": {c: int(sig[c].isna().sum()) for c in sig.columns},
              "nan_outcomes": {c: int(out[c].isna().sum()) for c in out.columns}}
    return {"pcr": pcr, "sig": sig, "out": out, "counts": counts}


def _d(x) -> str:
    return "-" if x is None else f"{x:%Y-%m-%d}"


def _nans(d: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in d.items())


def audit_lines(prep: dict) -> list[str]:
    """Counts only: no ratio, signal, return or statistic of any of them."""
    n = prep["counts"]
    return [(f"option rows {n['option_rows']} (ignored {n['option_rows_ignored']}), "
             f"option sessions {n['option_sessions']}"),
            f"spot sessions {n['spot_sessions']}, duplicate dates {n['spot_dupes']}",
            (f"joint sessions {n['joint_sessions']} ({_d(n['joint_first'])} to "
             f"{_d(n['joint_last'])}), option sessions without spot "
             f"{n['option_sessions_without_spot']}, spot sessions inside it without options "
             f"{n['spot_sessions_without_options']}"),
            (f"sample sessions {n['sample_sessions']} ({_d(n['sample_first'])} to "
             f"{_d(n['sample_last'])})"),
            "NaN levels in the sample: " + _nans(n["nan_pcr"]),
            "NaN signals in the sample: " + _nans(n["nan_signals"]),
            "NaN outcomes in the sample: " + _nans(n["nan_outcomes"])]


def report(prep: dict) -> dict:
    """The registered run: the 8-row table, survivors, constants, counts and the daily levels."""
    res = run_tests(prep["sig"], prep["out"])
    return {"tests": res.to_dict(orient="index"),
            "survivors": [k for k, v in res["survives"].items() if v],
            "constants": {"DEFS": DEFS, "TRANSFORMS": TRANSFORMS, "HORIZONS": HORIZONS,
                          "WINDOW": WINDOW, "CONTROLS": CONTROLS, "SAMPLE_FROM": SAMPLE_FROM,
                          "BAR_T": BAR_T, "ALPHA": ALPHA},
            "counts": prep["counts"],
            "pcr": {c: prep["pcr"][c] for c in prep["pcr"].columns}}


def _f(x, spec="+.3f") -> str:
    return "nan" if x is None or not math.isfinite(x) else format(x, spec)


def report_lines(rep: dict) -> list[str]:
    head = (f"{'test':<14} {'n':>5} {'IC':>7} {'NW t':>7} {'p holm':>7} {'partial':>8} "
            f"{'NW t':>7} {'p holm':>7}  survives")
    out = [f"8 tests, Holm over 8, bar |t| >= {BAR_T} and p < {ALPHA} on raw and partial", head]
    for k, r in rep["tests"].items():
        out.append(f"{k:<14} {r['n']:>5} {_f(r['ic']):>7} {_f(r['t'], '+.2f'):>7} "
                   f"{_f(r['p_holm'], '.3f'):>7} {_f(r['ic_partial']):>8} "
                   f"{_f(r['t_partial'], '+.2f'):>7} {_f(r['p_partial_holm'], '.3f'):>7}  "
                   f"{'YES' if r['survives'] else 'no'}")
    out.append("survivors: " + (", ".join(rep["survivors"]) or "none"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pcr_daily.py",
                                 description="Pre-registered daily NIFTY put-call ratio tests.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit", action="store_true",
                      help="counts only: sessions, sample range, NaN signals and outcomes")
    mode.add_argument("--run", action="store_true",
                      help="the one registered run: 8 tests, Holm, survivors")
    ap.add_argument("--out", type=Path,
                    help="JSON written by --run (default $SHUNKAN_HOME/research/"
                         "pcr_daily_run.json); an existing file is never overwritten")
    ap.add_argument("--refresh-cache", action="store_true",
                    help="re-read the bhavcopies into the option cache shared with vrp_condor")
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
    prep = assemble(opts, load_history("NIFTY"))
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
