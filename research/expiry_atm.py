"""ATM straddle panel from the contracts archive, by days-to-expiry."""
from __future__ import annotations
import numpy as np, pandas as pd
from research.expiry_day import load_panel, spot
from shunkan.derivatives.greeks import implied_vol

def atm_panel(min_vol: int = 1) -> pd.DataFrame:
    p = load_panel()
    p = p[p["volume"] >= min_vol]           # a close with no trade is a stale mark
    rows = []
    for sym, sub in p.groupby("symbol"):
        s = spot(sym)
        sub = sub.merge(s, on="date", how="inner")
        # pair CE/PE at the same strike/date
        ce = sub[sub["right"] == "CE"][["date","expiry","strike","close","volume","oi","lot_size","spot_close"]]
        pe = sub[sub["right"] == "PE"][["date","expiry","strike","close","volume","oi"]]
        m = ce.merge(pe, on=["date","expiry","strike"], suffixes=("_ce","_pe"))
        m["symbol"] = sym
        m["straddle"] = m["close_ce"] + m["close_pe"]
        m["moneyness"] = (m["strike"] - m["spot_close"]).abs()
        # ATM = the traded pair nearest spot on that date/expiry
        m = m.sort_values(["date","expiry","moneyness"])
        rows.append(m.groupby(["date","expiry"], as_index=False).first())
    a = pd.concat(rows, ignore_index=True)
    a["dte"] = (a["expiry"] - a["date"]).dt.days
    a["T"] = a["dte"] / 365.0
    a["strad_pct"] = a["straddle"] / a["spot_close"] * 100.0
    iv_c = implied_vol(a["close_ce"].values, a["spot_close"].values, a["strike"].values,
                       a["T"].values, True)
    iv_p = implied_vol(a["close_pe"].values, a["spot_close"].values, a["strike"].values,
                       a["T"].values, False)
    a["iv"] = np.nanmean(np.vstack([iv_c, iv_p]), axis=0) * 100.0
    return a

if __name__ == "__main__":
    pd.set_option("display.width", 220)
    a = atm_panel()
    print("ATM observations:", len(a))
    n = a[a["symbol"] == "NIFTY"]
    print("\n--- NIFTY ATM straddle by calendar DTE (all expiries pooled) ---")
    b = n.groupby(pd.cut(n["dte"], [0,1,2,3,5,7,10,14,21,30,45,60,90,180,400,3000]))
    print(b.agg(n=("straddle","size"), strad_pct=("strad_pct","mean"),
                iv=("iv","mean"), prem=("straddle","mean"),
                mn_ce_vol=("volume_ce","median")).to_string())
    print("\n--- NIFTY WEEKLY expiries only, dte<=10, day by day ---")
    wk = n[n["expiry"].isin(pd.to_datetime(["2026-08-18","2026-08-25","2026-09-01",
                                            "2026-09-08","2026-09-15"]))]
    print(wk[wk["dte"] <= 12][["date","expiry","dte","spot_close","strike","close_ce","close_pe",
        "straddle","strad_pct","iv","volume_ce","volume_pe","oi_ce","oi_pe"]]
        .sort_values(["expiry","dte"], ascending=[True,False]).to_string(index=False))
