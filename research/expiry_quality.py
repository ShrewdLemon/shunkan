"""Is a day candle's CE close and PE close the same instant? Put-call parity says."""
from __future__ import annotations
import numpy as np, pandas as pd
from research.expiry_atm import atm_panel

if __name__ == "__main__":
    pd.set_option("display.width", 240)
    a = atm_panel()
    a = a[a["date"] < pd.Timestamp("2026-08-17")]        # today's candle is partial
    # implied forward from the ATM pair; should sit at S*exp(rT), i.e. ~0 basis
    a["F"] = a["strike"] + a["close_ce"] - a["close_pe"]
    a["basis_pct"] = (a["F"] / a["spot_close"] - 1.0) * 100.0
    a["theo_basis_pct"] = (np.exp(0.065 * a["T"]) - 1.0) * 100.0
    a["basis_err"] = a["basis_pct"] - a["theo_basis_pct"]
    print("ATM obs after dropping today:", len(a))
    for sym in ["NIFTY", "BANKNIFTY"]:
        s = a[a["symbol"] == sym]
        print(f"\n=== {sym}: put-call-parity basis error, % of spot, by DTE ===")
        g = s.groupby(pd.cut(s["dte"], [0,3,7,14,30,60,120,400,3000]))
        print(g.agg(n=("basis_err","size"), mean=("basis_err","mean"),
                    med=("basis_err","median"), sd=("basis_err","std"),
                    p05=("basis_err", lambda x: x.quantile(.05)),
                    p95=("basis_err", lambda x: x.quantile(.95)),
                    abs_gt_0p2=("basis_err", lambda x: float((x.abs()>0.2).mean()))).to_string())
    print("\n=== how big is that in rupees vs the straddle itself? (NIFTY, dte<=14) ===")
    s = a[(a["symbol"]=="NIFTY") & (a["dte"]<=14)].copy()
    s["basis_rs"] = s["basis_err"]/100*s["spot_close"]
    print(s[["date","expiry","dte","spot_close","strike","close_ce","close_pe","straddle",
             "F","basis_err","basis_rs"]].sort_values("date").to_string(index=False))
    print("\nmean |basis| in Rs as %% of straddle premium: "
          f"{(s['basis_rs'].abs()/s['straddle']).mean()*100:.1f}%")
