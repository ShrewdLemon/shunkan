"""Try to put the best available near-expiry construction through validate.py."""
from __future__ import annotations
import numpy as np, pandas as pd
from research.expiry_atm import atm_panel
from shunkan.backtest.validate import permutation_test, deflated_sharpe
from shunkan.backtest.costs import Fill, round_trip

if __name__ == "__main__":
    pd.set_option("display.width", 220)
    a = atm_panel(); a = a[a["date"] < pd.Timestamp("2026-08-17")]
    a = a[(a["symbol"]=="NIFTY") & (a["dte"]<=21)].sort_values(["expiry","date"])
    # Short the front-weekly ATM straddle, held on the SAME strike, marked daily.
    # Strike fixed at entry -> no restriking lookahead.
    legs=[]
    for exp, g in a.groupby("expiry"):
        g = g.sort_values("date")
        k = g.iloc[0]["strike"]
        h = a[(a["expiry"]==exp) & (a["strike"]==k)].sort_values("date")
        if len(h) < 3: continue
        h = h.copy()
        h["pnl_pts"] = -h["straddle"].diff()          # short: premium falling = profit
        lot = int(h.iloc[0]["lot_size"])
        c = round_trip([Fill("SELL", h.iloc[0]["close_ce"], lot),
                        Fill("SELL", h.iloc[0]["close_pe"], lot)]).total
        h["pnl_rs"] = h["pnl_pts"]*lot
        h.iloc[-1, h.columns.get_loc("pnl_rs")] -= c   # charge the round trip once
        margin = h.iloc[0]["spot_close"]*lot*0.12      # ~12% SPAN+exposure on a short straddle
        h["ret"] = h["pnl_rs"]/margin
        legs.append(h[["date","expiry","dte","strike","straddle","pnl_rs","ret"]].iloc[1:])
    r = pd.concat(legs).sort_values("date")
    print("=== short front-weekly ATM straddle, fixed strike, marked daily, net of costs ===")
    print(r.to_string(index=False))
    print(f"\nbars {len(r)}   distinct dates {r['date'].nunique()}   "
          f"expiry cycles {r['expiry'].nunique()}")
    print(f"total P&L Rs {r['pnl_rs'].sum():,.0f}   mean daily ret {r['ret'].mean()*100:+.3f}%")

    print("\n--- permutation_test ---")
    try:
        pos = pd.Series(np.ones(len(r)))
        pr = permutation_test(pos, pd.Series(r["ret"].values), n_permutations=1000)
        print(pr.verdict())
    except Exception as e:
        print(f"REFUSED: {type(e).__name__}: {e}")
    print("--- deflated_sharpe ---")
    try:
        ds = deflated_sharpe(pd.Series(r["ret"].values), n_trials=12, periods=252)
        print(ds.verdict())
    except Exception as e:
        print(f"REFUSED: {type(e).__name__}: {e}")
