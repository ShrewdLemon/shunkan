"""Fair version: track the entry strike through the RAW panel, not the ATM filter."""
from __future__ import annotations
import numpy as np, pandas as pd
from research.expiry_day import load_panel, spot
from shunkan.backtest.validate import permutation_test, deflated_sharpe
from shunkan.backtest.costs import Fill, round_trip

if __name__ == "__main__":
    pd.set_option("display.width", 220)
    p = load_panel(); p = p[p["date"] < pd.Timestamp("2026-08-17")]
    p = p[p["symbol"]=="NIFTY"]
    s = spot("NIFTY"); p = p.merge(s, on="date")
    p["dte"] = (p["expiry"]-p["date"]).dt.days
    ce = p[p["right"]=="CE"][["date","expiry","strike","close","volume","lot_size","spot_close","dte"]]
    pe = p[p["right"]=="PE"][["date","expiry","strike","close","volume"]]
    m = ce.merge(pe, on=["date","expiry","strike"], suffixes=("_ce","_pe"))
    m["straddle"] = m["close_ce"]+m["close_pe"]

    out=[]
    for exp, g in m.groupby("expiry"):
        ent = g[(g["dte"]<=21) & (g["volume_ce"]>0) & (g["volume_pe"]>0)]
        if ent.empty: continue
        d0 = ent["date"].min()
        row = ent[ent["date"]==d0].assign(mn=lambda x:(x["strike"]-x["spot_close"]).abs()).nsmallest(1,"mn").iloc[0]
        k = row["strike"]
        h = g[(g["strike"]==k) & (g["date"]>=d0)].sort_values("date").copy()
        h = h[(h["volume_ce"]>0) | (h["volume_pe"]>0)]
        if len(h) < 3: continue
        lot = int(h.iloc[0]["lot_size"])
        c = round_trip([Fill("SELL", h.iloc[0]["close_ce"], lot),
                        Fill("SELL", h.iloc[0]["close_pe"], lot)]).total
        h["pnl_rs"] = -h["straddle"].diff()*lot
        h.iloc[-1, h.columns.get_loc("pnl_rs")] -= c
        h["ret"] = h["pnl_rs"]/(h.iloc[0]["spot_close"]*lot*0.12)
        out.append(h[["date","expiry","dte","strike","straddle","pnl_rs","ret"]].iloc[1:])
    r = pd.concat(out).sort_values("date")
    print(r.to_string(index=False))
    print(f"\nbars {len(r)}  dates {r['date'].nunique()}  cycles {r['expiry'].nunique()}  "
          f"min dte reached {int(r['dte'].min())}")
    print(f"total P&L Rs {r['pnl_rs'].sum():,.0f}   mean daily ret {r['ret'].mean()*100:+.3f}%  "
          f"sd {r['ret'].std()*100:.3f}%")
    for name, fn in (("permutation_test", lambda: permutation_test(
                        pd.Series(np.ones(len(r))), pd.Series(r["ret"].values), n_permutations=1000)),
                     ("deflated_sharpe", lambda: deflated_sharpe(
                        pd.Series(r["ret"].values), n_trials=12, periods=252))):
        try:
            print(f"--- {name}: {fn().verdict()}")
        except Exception as e:
            print(f"--- {name}: REFUSED {type(e).__name__}: {e}")
