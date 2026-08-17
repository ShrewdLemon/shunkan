"""What the expiry-day terminal move actually is — the payout leg of every
expiry-day short. Open-to-close, because that is the window an expiry-morning
seller is exposed to."""
from __future__ import annotations
import numpy as np, pandas as pd
from research.expiry_underlying import nifty, expiry_flags, WEEKLY_START

if __name__ == "__main__":
    pd.set_option("display.width", 220)
    d = nifty(); d = d.merge(expiry_flags(d["date"]), on="date").dropna(subset=["ret"])
    d["oc"] = d["close"]/d["open"] - 1.0
    d["hl"] = d["high"]/d["low"] - 1.0

    for name, sub, flag in [("monthly 2007-2026", d, "is_monthly"),
                            ("weekly 2019-2026", d[d["date"]>=WEEKLY_START], "is_weekly")]:
        e = sub[sub[flag]]; o = sub[~sub[flag]]
        print(f"\n=== {name}: OPEN-to-CLOSE move on expiry day vs other days ===")
        print(f"n {len(e)} vs {len(o)}")
        m1,m2 = e["oc"].abs().mean(), o["oc"].abs().mean()
        s1,s2 = e["oc"].abs().std(ddof=1), o["oc"].abs().std(ddof=1)
        t = (m1-m2)/np.sqrt(s1**2/len(e)+s2**2/len(o))
        print(f"mean |open->close| expiry {m1*100:.4f}%   other {m2*100:.4f}%   "
              f"ratio {m1/m2:.4f}   Welch t={t:+.2f}")
        print(f"median |o->c| expiry {e['oc'].abs().median()*100:.4f}%  other {o['oc'].abs().median()*100:.4f}%")
        print(f"mean high-low range expiry {e['hl'].mean()*100:.4f}%  other {o['hl'].mean()*100:.4f}%  "
              f"ratio {e['hl'].mean()/o['hl'].mean():.4f}")
        for k in (0.005, 0.01, 0.015):
            pe_, po_ = (e["oc"].abs()>k).mean(), (o["oc"].abs()>k).mean()
            payout_e = np.maximum(e["oc"].abs()-k, 0).mean()
            print(f"  P(|o->c| > {k*100:.1f}%): expiry {pe_*100:5.2f}%  other {po_*100:5.2f}%   "
                  f"| mean payout of a {k*100:.1f}%-OTM strangle leg on expiry day: "
                  f"{payout_e*100:.4f}% of spot")

    e = d[d["date"]>=WEEKLY_START]; e = e[e["is_weekly"]]
    spot = 24254.05
    print(f"\n=== what an expiry-MORNING 1%-OTM NIFTY option must cost to be a fair sale ===")
    for k in (0.005, 0.01):
        payout = np.maximum(e["oc"].abs()-k, 0).mean()
        # a strangle sells both wings; only one can pay
        print(f"  {k*100:.1f}%-OTM single wing: mean terminal payout {payout*spot:.2f} index pts "
              f"({payout*100:.4f}% of spot), pays at all {(e['oc'].abs()>k).mean()*100:.1f}% of the time")
    print("\nThe archive holds ZERO expiry-day option prices, so the premium side of this "
          "comparison cannot be observed. Payout measured, price unobservable.")
