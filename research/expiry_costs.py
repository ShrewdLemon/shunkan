"""Break-even premium for an expiry-day wing sale, against the measured payout.

Two settlement paths, because they cost very different amounts and costs.py
only models one of them:
  CLOSED  - bought back before the bell: round_trip(), 2 orders per leg
  EXPIRED - allowed to lapse/settle: cost_of() on the open only, 1 order
"""
from __future__ import annotations
import numpy as np
from shunkan.backtest.costs import Fill, cost_of, round_trip

SPOT, LOT = 24254.05, 65
PAYOUT_1PCT = 21.06     # index pts, measured: research/expiry_terminal.py
PAYOUT_0P5  = 54.33

def breakeven(payout_pts: float, lots: int, expired: bool) -> float:
    q = LOT*lots
    lo, hi = 0.01, 500.0
    for _ in range(200):
        p = 0.5*(lo+hi)
        legs = [Fill("SELL", p, q)]
        c = cost_of(legs).total if expired else round_trip(legs).total
        pnl = (p - payout_pts)*q - c
        if pnl > 0: hi = p
        else: lo = p
    return 0.5*(lo+hi)

if __name__ == "__main__":
    print(f"measured mean terminal payout, NIFTY weekly expiry day (n=392, 2019-2026):")
    print(f"  1.0%-OTM wing {PAYOUT_1PCT:.2f} pts   0.5%-OTM wing {PAYOUT_0P5:.2f} pts")
    print(f"\nlot {LOT}, spot {SPOT:,.0f}\n")
    print(f"{'wing':>8} {'lots':>5} {'settle':>8} {'breakeven prem':>15} {'vs payout':>10} {'cost drag':>10}")
    for name, payout in (("1.0%-OTM", PAYOUT_1PCT), ("0.5%-OTM", PAYOUT_0P5)):
        for lots in (1, 10, 50):
            for expired in (True, False):
                b = breakeven(payout, lots, expired)
                print(f"{name:>8} {lots:>5} {'expired' if expired else 'closed':>8} "
                      f"{b:>15.2f} {b/payout-1:>+9.1%} {(b-payout)*LOT*lots:>10,.0f}")
    print("\nSAME structure as a two-legged expiry-day strangle (both wings), 1 lot, closed:")
    q = LOT
    for prem in (10, 20, 30, 50):
        legs = [Fill("SELL", prem, q), Fill("SELL", prem, q)]
        rt = round_trip(legs); op = cost_of(legs)
        cr = 2*prem*q
        print(f"  credit {prem*2:>5.0f} pts (Rs {cr:>7,.0f}):  round-trip cost Rs {rt.total:>6,.0f} "
              f"= {rt.total/cr:>6.2%} of credit   |  let-expire cost Rs {op.total:>6,.0f} = {op.total/cr:>6.2%}")
    print("\nNOTE costs.py:80 charges STT_EXERCISE_INTRINSIC whenever Fill.intrinsic>0 with no")
    print("side check, though the docstring at costs.py:81-82 says it applies to longs only.")
    f_short = cost_of([Fill("SELL", 30.0, LOT, intrinsic=100.0)])
    f_long  = cost_of([Fill("BUY",  30.0, LOT, intrinsic=100.0)])
    print(f"  cost_of(SELL, intrinsic=100) = Rs {f_short.total:,.2f}  "
          f"cost_of(BUY, intrinsic=100) = Rs {f_long.total:,.2f}  -> identical STT of "
          f"Rs {0.00125*100*LOT:,.2f} on both")
