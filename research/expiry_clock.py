"""The near-expiry IV 'collapse' on a calendar clock vs a trading-day clock."""
from __future__ import annotations
import numpy as np, pandas as pd
from pathlib import Path
from research.expiry_atm import atm_panel
from shunkan.derivatives.greeks import implied_vol

HIST = Path.home()/".shunkan/store/history"
# NSE holidays that fall on a weekday between 2026-07-15 and 2026-09-15.
# Aug 15 2026 (Independence Day) is a Saturday; none of the Sep festivals land
# inside the windows used here. Stated so the assumption is auditable.
HOLIDAYS = pd.to_datetime([])

def tdays(d, e):
    """Trading days from d (exclusive) to expiry e (inclusive)."""
    b = pd.bdate_range(d + pd.Timedelta(days=1), e)
    return int(len(b.difference(HOLIDAYS)))

if __name__ == "__main__":
    pd.set_option("display.width", 250)
    vix = pd.read_parquet(HIST/"_INDIAVIX.parquet")[["date","close"]].rename(columns={"close":"vix"})
    vix["date"] = pd.to_datetime(vix["date"])
    a = atm_panel(); a = a[a["date"] < pd.Timestamp("2026-08-17")]
    a = a[a["symbol"]=="NIFTY"].merge(vix, on="date").copy()
    a["td"] = [tdays(d, e) for d, e in zip(a["date"], a["expiry"])]
    a["T_td"] = a["td"]/252.0
    ivc = implied_vol(a["close_ce"].values, a["spot_close"].values, a["strike"].values, a["T_td"].values, True)
    ivp = implied_vol(a["close_pe"].values, a["spot_close"].values, a["strike"].values, a["T_td"].values, False)
    a["iv_td"] = np.nanmean(np.vstack([ivc, ivp]), axis=0)*100.0

    f = a[a["expiry"]==pd.Timestamp("2026-08-18")].sort_values("date").copy()
    f["iv_cal_over_vix"] = f["iv"]/f["vix"]
    f["iv_td_over_vix"]  = f["iv_td"]/f["vix"]
    print("=== NIFTY 2026-08-18: calendar-clock IV vs trading-day-clock IV ===")
    print(f[["date","dte","td","straddle","iv","iv_td","vix","iv_cal_over_vix","iv_td_over_vix"]]
          .to_string(index=False))
    lo, hi = f.iloc[0], f.iloc[-1]
    print(f"\ncalendar clock : IV {lo['iv']:.2f} -> {hi['iv']:.2f}  ({(hi['iv']/lo['iv']-1)*100:+.1f}%)")
    print(f"trading clock  : IV {lo['iv_td']:.2f} -> {hi['iv_td']:.2f}  ({(hi['iv_td']/lo['iv_td']-1)*100:+.1f}%)")
    print(f"India VIX      :    {lo['vix']:.2f} -> {hi['vix']:.2f}  ({(hi['vix']/lo['vix']-1)*100:+.1f}%)")
    print(f"\nIV/VIX ratio, trading clock: {lo['iv_td_over_vix']:.3f} -> {hi['iv_td_over_vix']:.3f}"
          f"   (residual excess decay = {(hi['iv_td_over_vix']/lo['iv_td_over_vix']-1)*100:+.1f}%)")
    print(f"IV/VIX ratio, calendar clock: {lo['iv_cal_over_vix']:.3f} -> {hi['iv_cal_over_vix']:.3f}"
          f"   (apparent excess decay  = {(hi['iv_cal_over_vix']/lo['iv_cal_over_vix']-1)*100:+.1f}%)")

    print("\n\n=== front-minus-next weekly ATM IV, both clocks ===")
    piv_c = a.pivot_table(index="date", columns="expiry", values="iv")
    piv_t = a.pivot_table(index="date", columns="expiry", values="iv_td")
    dt = a.pivot_table(index="date", columns="expiry", values="dte")
    td = a.pivot_table(index="date", columns="expiry", values="td")
    rows=[]
    for d in piv_c.index:
        av = sorted([(e, dt.loc[d,e]) for e in piv_c.columns if np.isfinite(piv_c.loc[d,e])],
                    key=lambda x: x[1])
        if len(av)>=2 and av[0][1] <= 21:
            e0,e1 = av[0][0], av[1][0]
            rows.append(dict(date=d, f_dte=int(dt.loc[d,e0]), f_td=int(td.loc[d,e0]),
                             n_dte=int(dt.loc[d,e1]), n_td=int(td.loc[d,e1]),
                             spread_cal=piv_c.loc[d,e0]-piv_c.loc[d,e1],
                             spread_td =piv_t.loc[d,e0]-piv_t.loc[d,e1]))
    t = pd.DataFrame(rows)
    print(t.to_string(index=False))
    print(f"\nmean spread, calendar clock: {t['spread_cal'].mean():+.3f} vol pts (n={len(t)})")
    print(f"mean spread, trading  clock: {t['spread_td'].mean():+.3f} vol pts (n={len(t)})")
    s = t[t['f_dte']<=8]
    print(f"front_dte<=8 : calendar {s['spread_cal'].mean():+.3f}  trading {s['spread_td'].mean():+.3f}  (n={len(s)})")
