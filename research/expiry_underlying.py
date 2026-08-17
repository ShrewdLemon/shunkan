"""Expiry day on the UNDERLYING, where there is actually a sample.

The option archive holds zero expiry days. The index does not: NIFTY daily
history runs to 4,640 sessions. Two questions that bear directly on whether an
expiry-day straddle is a good sale:
  (a) is there directional drift on expiry day?   -> validate.py
  (b) is the expiry-day MOVE smaller (pinning)?   -> that is what a short
      straddle is actually betting on
"""
from __future__ import annotations
import numpy as np, pandas as pd
from pathlib import Path
from shunkan.backtest.validate import permutation_test, deflated_sharpe, sharpe

HIST = Path.home()/".shunkan/store/history"
SWITCH = pd.Timestamp("2025-09-01")   # NSE moved NIFTY expiry Thu -> Tue
WEEKLY_START = pd.Timestamp("2019-02-11")   # NIFTY weekly options launch


def nifty() -> pd.DataFrame:
    d = pd.read_parquet(HIST/"_NSEI.parquet")[["date","open","high","low","close"]]
    d["date"] = pd.to_datetime(d["date"])
    d = d[d["date"] < pd.Timestamp("2026-08-17")].sort_values("date").reset_index(drop=True)
    d["ret"] = d["close"].pct_change()
    return d


def expiry_flags(dates: pd.Series) -> pd.DataFrame:
    """Weekly/monthly expiry from the trading calendar itself: the last trading
    day of each week at or before that era's expiry weekday. No lookahead — the
    calendar is published years ahead."""
    d = pd.DataFrame({"date": dates})
    d["dow"] = d["date"].dt.dayofweek
    d["wk"] = d["date"].dt.isocalendar().week.astype(int)
    d["yr"] = d["date"].dt.isocalendar().year.astype(int)
    tgt = np.where(d["date"] < SWITCH, 3, 1)          # Thu=3, Tue=1
    d["tgt"] = tgt
    ok = d["dow"] <= d["tgt"]
    d["is_weekly"] = False
    idx = d[ok].groupby(["yr","wk"])["date"].idxmax()
    d.loc[idx, "is_weekly"] = True
    d["ym"] = d["date"].dt.to_period("M")
    d["is_monthly"] = False
    midx = d[d["is_weekly"]].groupby("ym")["date"].idxmax()
    d.loc[midx, "is_monthly"] = True
    return d[["date","is_weekly","is_monthly"]]


def futures_rt_bps(notional: float) -> float:
    """NIFTY futures round trip, bps of notional. costs.py models options only
    (Fill charges STT_SELL_PREMIUM on premium turnover, costs.py:76-79), so the
    futures schedule is spelled out here rather than misapplied."""
    brokerage = 40.0
    stt = 0.0002 * notional            # sell side, futures
    exch = 0.0000173 * 2 * notional
    sebi = 0.000001 * 2 * notional
    stamp = 0.00002 * notional         # buy side
    gst = 0.18 * (brokerage + exch + sebi)
    return (brokerage+stt+exch+sebi+stamp+gst)/notional*1e4


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    d = nifty().merge(expiry_flags(nifty()["date"]), on="date")
    d = d.dropna(subset=["ret"])
    lot_notional = 24254.05*65
    rt = futures_rt_bps(lot_notional)
    print(f"NIFTY futures round trip at 1 lot (Rs {lot_notional:,.0f} notional): {rt:.2f} bps")
    print(f"sessions {len(d):,}  {d['date'].min().date()} .. {d['date'].max().date()}")

    for era_name, mask in [("monthly expiry, 2007-2026", d["is_monthly"]),
                           ("weekly expiry, 2019-2026", d["is_weekly"] & (d["date"]>=WEEKLY_START))]:
        sub = d if "monthly" in era_name else d[d["date"]>=WEEKLY_START]
        pos = mask.reindex(sub.index).fillna(False).astype(float)
        ret_net = sub["ret"] - np.sign(pos)*rt/1e4
        act = sub.loc[pos>0, "ret"]
        act_net = act - rt/1e4
        oth = sub.loc[pos==0, "ret"]
        per_yr = 12 if "monthly" in era_name else 52
        print(f"\n=== {era_name} ===")
        print(f"n expiry days {len(act)}   n other {len(oth)}")
        print(f"mean ret on expiry day  {act.mean()*100:+.4f}%  (net {act_net.mean()*100:+.4f}%)"
              f"   sd {act.std()*100:.3f}%   t={act_net.mean()/(act_net.std(ddof=1)/np.sqrt(len(act))):.2f}")
        print(f"mean ret other days     {oth.mean()*100:+.4f}%   sd {oth.std()*100:.3f}%")
        print(f"MEAN |ret| expiry day   {act.abs().mean()*100:.4f}%   other {oth.abs().mean()*100:.4f}%"
              f"   ratio {act.abs().mean()/oth.abs().mean():.4f}")
        # Welch t on |ret| — the number a straddle seller actually cares about
        m1,m2 = act.abs().mean(), oth.abs().mean()
        s1,s2 = act.abs().std(ddof=1), oth.abs().std(ddof=1)
        tw = (m1-m2)/np.sqrt(s1**2/len(act)+s2**2/len(oth))
        print(f"Welch t on |ret| (expiry vs other) = {tw:+.2f}")
        pr = permutation_test(pd.Series(pos.values), pd.Series(ret_net.values),
                              n_permutations=2000, block_size=20, periods=252)
        print(f"permutation: obs Sharpe {pr.observed_sharpe:.3f}  p={pr.p_value:.4f}  -> {pr.verdict()}")
        ds = deflated_sharpe(pd.Series(act_net.values), n_trials=1, periods=per_yr)
        print(f"deflated (n_trials=1, best case): Sharpe {ds.observed_sharpe:.3f} DSR={ds.deflated:.4f}")
