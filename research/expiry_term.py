"""Is the near-expiry IV decline an expiry effect, or just India VIX falling?
Cross-sectional front-vs-next weekly is the discriminator."""
from __future__ import annotations
import numpy as np, pandas as pd
from pathlib import Path
from research.expiry_atm import atm_panel

HIST = Path.home()/".shunkan/store/history"

if __name__ == "__main__":
    pd.set_option("display.width", 240)
    vix = pd.read_parquet(HIST/"_INDIAVIX.parquet")[["date","close"]].rename(columns={"close":"vix"})
    vix["date"] = pd.to_datetime(vix["date"])
    a = atm_panel(); a = a[a["date"] < pd.Timestamp("2026-08-17")]
    n = a[a["symbol"]=="NIFTY"].merge(vix, on="date")
    n["iv_over_vix"] = n["iv"]/n["vix"]

    front = n[n["expiry"]==pd.Timestamp("2026-08-18")]
    print("=== NIFTY 2026-08-18 front weekly: ATM IV vs India VIX, same days ===")
    print(front[["date","dte","iv","vix","iv_over_vix","straddle"]].sort_values("date").to_string(index=False))
    c = np.corrcoef(front["iv"], front["vix"])[0,1]
    print(f"\ncorr(front ATM IV, VIX) = {c:.3f} over n={len(front)}")
    print(f"VIX  {front.sort_values('date').iloc[0]['vix']:.2f} -> {front.sort_values('date').iloc[-1]['vix']:.2f}"
          f"   ({(front.sort_values('date').iloc[-1]['vix']/front.sort_values('date').iloc[0]['vix']-1)*100:+.1f}%)")
    print(f"IV   {front.sort_values('date').iloc[0]['iv']:.2f} -> {front.sort_values('date').iloc[-1]['iv']:.2f}"
          f"   ({(front.sort_values('date').iloc[-1]['iv']/front.sort_values('date').iloc[0]['iv']-1)*100:+.1f}%)")

    print("\n\n=== CROSS-SECTION: on the SAME day, front weekly vs next expiry ===")
    piv = n.pivot_table(index="date", columns="expiry", values="iv")
    dtes = n.pivot_table(index="date", columns="expiry", values="dte")
    rows=[]
    for d in piv.index:
        avail = [(e, dtes.loc[d,e], piv.loc[d,e]) for e in piv.columns if np.isfinite(piv.loc[d,e])]
        avail.sort(key=lambda x: x[1])
        if len(avail) >= 2 and avail[0][1] <= 21:
            rows.append({"date": d, "front_exp": avail[0][0].date(), "front_dte": int(avail[0][1]),
                         "front_iv": avail[0][2], "next_exp": avail[1][0].date(),
                         "next_dte": int(avail[1][1]), "next_iv": avail[1][2],
                         "spread": avail[0][2]-avail[1][2]})
    t = pd.DataFrame(rows)
    print(t.to_string(index=False))
    if len(t):
        print(f"\nfront-minus-next ATM IV: mean {t['spread'].mean():+.3f} vol pts, "
              f"median {t['spread'].median():+.3f}, sd {t['spread'].std():.3f}, n={len(t)}, "
              f"negative on {int((t['spread']<0).sum())}/{len(t)} days")
        se = t["spread"].std(ddof=1)/np.sqrt(len(t))
        print(f"naive t-stat = {t['spread'].mean()/se:.2f}  "
              "(OVERLAPPING: consecutive days share the same two contracts, so this is inflated)")
        sub = t[t["front_dte"] <= 8]
        if len(sub):
            print(f"\nrestricted to front_dte<=8: mean {sub['spread'].mean():+.3f}, n={len(sub)}")
