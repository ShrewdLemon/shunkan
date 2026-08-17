"""How many near-expiry observations does the archive actually support?"""
from __future__ import annotations
import numpy as np, pandas as pd
from research.expiry_day import load_panel
from research.expiry_atm import atm_panel

if __name__ == "__main__":
    pd.set_option("display.width", 200)
    p = load_panel()
    p["dte"] = (p["expiry"] - p["date"]).dt.days
    live = p[p["date"] < pd.Timestamp("2026-08-17")]
    print("=== every option candle in the archive, by DTE bucket (today dropped) ===")
    print(live.groupby(pd.cut(live["dte"], [-1,0,1,2,3,4,5,7,10,14,21,30,60,3000]))
              .agg(candles=("close","size"), traded=("volume", lambda v: int((v>0).sum())),
                   contracts=("tradingsymbol","nunique"),
                   dates=("date","nunique")).to_string())
    print("\nrows at dte<=3 (any symbol, any strike, complete days):",
          int((live["dte"] <= 3).sum()))
    print("rows at dte<=3 including today's partial bar:", int((p["dte"] <= 3).sum()))

    a = atm_panel(); a = a[a["date"] < pd.Timestamp("2026-08-17")]
    print("\n=== ATM straddle observations at dte<=21, by (symbol, expiry) ===")
    s = a[a["dte"] <= 21]
    print(s.groupby(["symbol","expiry"]).agg(n=("dte","size"), min_dte=("dte","min"),
          max_dte=("dte","max"), first=("date","min"), last=("date","max")).to_string())
    print(f"\nTOTAL ATM obs at dte<=21: {len(s)}   distinct calendar dates: {s['date'].nunique()}")
    print(f"TOTAL ATM obs at dte<=7 : {int((a['dte']<=7).sum())}   "
          f"distinct dates: {a[a['dte']<=7]['date'].nunique()}")
    print("\nvalidate.py minimums: permutation_test needs block_size*4 = 80 bars "
          "(validate.py:108); deflated_sharpe needs 20 returns (validate.py:178)")
