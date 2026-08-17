"""Observed near-expiry ATM decay vs Black-Scholes, and the cost stack on the
premiums actually observed."""
from __future__ import annotations
import numpy as np, pandas as pd
from research.expiry_atm import atm_panel
from shunkan.backtest.costs import Fill, cost_of, round_trip, breakeven_edge

if __name__ == "__main__":
    pd.set_option("display.width", 240)
    a = atm_panel(); a = a[a["date"] < pd.Timestamp("2026-08-17")]
    n = a[(a["symbol"]=="NIFTY") & (a["expiry"]==pd.Timestamp("2026-08-18"))].sort_values("dte", ascending=False)
    # BS benchmark: hold the FIRST day's ATM IV fixed and reprice at each later dte
    iv0 = n.iloc[0]["iv"]/100.0
    from shunkan.derivatives.greeks import bs_price
    n = n.copy()
    n["bs_flat_iv"] = (bs_price(n["spot_close"].values, n["strike"].values, n["T"].values, iv0, True)
                     + bs_price(n["spot_close"].values, n["strike"].values, n["T"].values, iv0, False))
    n["obs_over_bs"] = n["straddle"]/n["bs_flat_iv"]
    print("=== NIFTY 2026-08-18 cycle: observed ATM straddle vs BS at frozen day-1 IV "
          f"({iv0*100:.2f}%) ===")
    print(n[["date","dte","spot_close","strike","straddle","strad_pct","iv",
             "bs_flat_iv","obs_over_bs"]].to_string(index=False))

    print("\n=== sqrt-time check: straddle / sqrt(dte), which is flat under constant IV ===")
    n["per_sqrt_day"] = n["straddle"]/np.sqrt(n["dte"])
    print(n[["date","dte","straddle","per_sqrt_day","iv"]].to_string(index=False))

    print("\n\n=== COST STACK on the premiums actually observed (NIFTY lot = 75) ===")
    lot = int(n.iloc[0]["lot_size"])
    print(f"lot_size read from archive: {lot}")
    obs = [(int(r.dte), float(r.close_ce), float(r.close_pe), float(r.straddle))
           for r in n.itertuples()]
    # plus the only dte<=1 print we have, and hypothetical expiry-morning levels
    print(f"\n{'dte':>4} {'premium':>9} {'credit@1lot':>12} {'RT cost':>9} {'%prem':>7} "
          f"{'@50lot %':>9} {'open-only %':>12}")
    for dte, ce, pe, strad in obs:
        for lots, tag in ((1,""),):
            q = lot*lots
            legs = [Fill("SELL", ce, q), Fill("SELL", pe, q)]
            rt = round_trip(legs); credit = strad*q
            q50 = lot*50
            legs50 = [Fill("SELL", ce, q50), Fill("SELL", pe, q50)]
            rt50 = round_trip(legs50)
            op = cost_of(legs)
            print(f"{dte:>4} {strad:>9.2f} {credit:>12,.0f} {rt.total:>9,.0f} "
                  f"{rt.total/credit*100:>6.2f}% {rt50.total/(strad*q50)*100:>8.2f}% "
                  f"{op.total/credit*100:>11.2f}%")
    print("\nsame table for hypothetical expiry-morning premiums (no such data exists):")
    for strad in (150.0, 100.0, 70.0, 50.0, 30.0):
        ce = pe = strad/2
        q = lot
        legs = [Fill("SELL", ce, q), Fill("SELL", pe, q)]
        rt = round_trip(legs); credit = strad*q
        q50 = lot*50
        rt50 = round_trip([Fill("SELL", ce, q50), Fill("SELL", pe, q50)])
        print(f"{'--':>4} {strad:>9.2f} {credit:>12,.0f} {rt.total:>9,.0f} "
              f"{rt.total/credit*100:>6.2f}% {rt50.total/(strad*q50)*100:>8.2f}%")
