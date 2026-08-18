"""Does participant positioning predict next-day NIFTY? The folklore test.

Run: .venv/bin/python research/participant_signal.py

Pre-registered before any number was computed:

H1 (the folklore): FII adding net-long index futures today predicts NIFTY up
tomorrow. Every Indian market wrap implies it; nobody in that ecosystem
publishes a t-stat.
H2: the effect, if any, is monotone in the SIZE of the change (z-scored).
H3 (fade-retail): Client net change predicts with the OPPOSITE sign.

Design and honest limits:
- Signal: day t's change in direction-sign net exposure (futures and options
  separately, four participants = 8 series). The file publishes ~18:00 IST
  on day t, after the close.
- Outcome, tradeable path: enter next OPEN (t+1), exit the open after
  (t+2). ln(open2/open1). No close-to-close flattery: you cannot trade
  yesterday's close on today's news.
- Alignment by position on the joint trading-day index, never calendar
  arithmetic across holidays.
- 255 published days = ~1 year. A daily-horizon screen on one year is a
  SCREEN, not a result; whatever survives here earns a longer backfill
  (the archive serves years) before anyone sizes it.
- 8 series x 2 reads (sign, quartile) are ~16 looks. Anything with |t| < 2.5
  here is noise until proven otherwise; DSR with trials counted applies to
  any strategy built from a survivor.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from shunkan.data.participant import store_path
from shunkan.store.store import STORE_DIR


def t_stat(x):
    n = len(x)
    if n < 8:
        return None
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / math.sqrt(n))) if sd > 1e-12 else None


def main() -> None:
    part = pd.read_parquet(store_path())
    nifty = (pd.read_parquet(STORE_DIR / "history" / "_NSEI.parquet")
             .sort_values("date").set_index("date"))
    nifty.index = pd.DatetimeIndex(nifty.index).tz_localize(None)
    lopen = np.log(nifty["open"][nifty["open"] > 0])

    wide = {}
    for who in ("FII", "Client", "DII", "Pro"):
        sub = (part[part.client_type == who].set_index("date")
               [["idx_fut_net", "idx_opt_net"]].sort_index())
        sub.index = pd.DatetimeIndex(sub.index).tz_localize(None)
        for col in ("idx_fut_net", "idx_opt_net"):
            wide[f"{who}.{col}"] = sub[col]
    sig = pd.DataFrame(wide)

    # Joint trading days: participant publication days that also priced.
    days = sig.index.intersection(lopen.index)
    sig = sig.loc[days]
    print(f"joint days: {len(days)}  {days[0].date()} → {days[-1].date()}\n")

    # Forward tradeable return per signal day t: open(t+1) -> open(t+2),
    # by POSITION in the price series (holidays respected).
    pos_of = {ts: i for i, ts in enumerate(lopen.index)}
    fwd = {}
    for t in days:
        i = pos_of[t]
        if i + 2 < len(lopen):
            fwd[t] = float(lopen.iloc[i + 2] - lopen.iloc[i + 1])
    fwd = pd.Series(fwd).reindex(days)

    print(f"{'series':22s} {'n':>4} {'sign-read: up-mean':>18} {'down-mean':>10} "
          f"{'spread t':>9}   quartile means (bp, Q1→Q4 of Δz)")
    for name, s in sig.items():
        chg = s.diff()
        z = (chg - chg.rolling(63, min_periods=21).mean()) / chg.rolling(63, min_periods=21).std()
        df = pd.DataFrame({"chg": chg, "z": z, "fwd": fwd}).dropna()
        if len(df) < 60:
            print(f"{name:22s} too few days"); continue
        up, dn = df.fwd[df.chg > 0], df.fwd[df.chg < 0]
        spread = df.fwd.where(df.chg > 0, -df.fwd)      # long-if-up, short-if-down
        t_spr = t_stat(spread.to_numpy())
        try:
            qlab = pd.qcut(df.z, 4, labels=False, duplicates="drop")
            qm = [f"{df.fwd[qlab == q].mean() * 1e4:+.1f}" for q in range(int(qlab.max()) + 1)]
        except ValueError:
            qm = ["-"]
        print(f"{name:22s} {len(df):>4} {up.mean() * 1e4:>+16.1f}bp {dn.mean() * 1e4:>+8.1f}bp "
              f"{t_spr if t_spr is None else format(t_spr, '+.2f'):>9}   {' '.join(qm)}")

    print("\nread with: one year, ~16 looks; |t| < 2.5 is noise here. NIFTY fut")
    print("round trip ~2-3bp. Publication ~18:00 IST day t; entry next open.")


if __name__ == "__main__":
    main()
