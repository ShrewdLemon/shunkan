"""Grade the daily analysis: D's claims vs D+1's tape, systematically.

Run: .venv/bin/python research/grade_analysis.py   (server must be up)

What the manual exercise of 2026-08-18 did for one day, done for a window:
each past day's analysis is pulled through the app's own replay endpoint
(the JOURNAL when one was recorded, reconstruction otherwise - the payload
says which), the gradable claims are extracted, and the next session's
archive scores them. No claim is graded that the analysis did not actually
make; a day with no captured chain gets dashes for the positioning claims,
not improvised ones.

Claims and their grading rules, fixed before running:
- RANGE REGIME: claimed when the day was ordinary (|z| < 2) and VIX sat
  below its 25th percentile. HELD if |D+1 close move| < 1 trailing sigma.
- PAIN PULL: the folk claim behind max pain. HELD if D+1's close ended
  NEARER the pain level than D's close was, FAILED if farther.
- OI SUPPORT: HELD if D+1's low stayed at/above the put wall; MIXED if it
  broke intraday but closed back above; FAILED if it closed below.
- OI RESIST: mirrored.
"""

from __future__ import annotations

import json
import urllib.request

import numpy as np
import pandas as pd

from shunkan.store.store import STORE_DIR

BASE = "http://127.0.0.1:8720"
N_PAIRS = 7


def replay(day: str) -> dict:
    with urllib.request.urlopen(f"{BASE}/api/analysis/daily/NIFTY?on={day}", timeout=60) as r:
        return json.load(r)


def main() -> None:
    hist = pd.read_parquet(STORE_DIR / "history" / "_NSEI.parquet").sort_values("date")
    hist["date"] = pd.to_datetime(hist["date"])
    hist = hist.set_index("date")
    ret = np.log(hist["close"]).diff()
    sigma = ret.rolling(63).std()

    days = list(hist.index[-(N_PAIRS + 1):])
    print(f"grading {len(days) - 1} pairs, {days[0].date()} → {days[-1].date()}, "
          f"through the app's replay endpoint\n")
    tally: dict[str, dict[str, int]] = {}

    def grade(name, verdict):
        t = tally.setdefault(name, {})
        t[verdict] = t.get(verdict, 0) + 1
        return verdict

    hdr = f"{'day D':10s} {'src':14s} {'regime':8s} {'pain pull':22s} {'support':18s} {'resist':18s}"
    print(hdr); print("-" * len(hdr))
    for i in range(len(days) - 1):
        d, nxt = days[i], days[i + 1]
        a = replay(d.date().isoformat())
        src = "journal" if str(a.get("served_from", "")).startswith("journal") else \
              "live" if a.get("served_from") == "live" else "reconstr"
        pos, vol, ev = a.get("positioning", {}), a.get("vol", {}), a.get("events", {})
        row_d, row_n = hist.loc[d], hist.loc[nxt]
        move = float(np.log(row_n["close"] / row_d["close"]))
        sig = float(sigma.loc[d]) if not np.isnan(sigma.loc[d]) else None

        # regime
        ordinary = (ev.get("today") or {}).get("classification") == "ordinary"
        low_vix = (vol.get("vix_pctile") or 100) < 25
        if ordinary and low_vix and sig:
            regime = grade("regime", "HELD" if abs(move) < sig else "FAILED")
        else:
            regime = "—"

        # pain pull
        pain = pos.get("max_pain")
        if pain and pos.get("spot"):
            before = abs(float(row_d["close"]) - pain)
            after = abs(float(row_n["close"]) - pain)
            pain_s = grade("pain pull",
                           "HELD" if after < before else "FAILED")
            pain_s = f"{pain_s} ({pain:.0f}: {before:.0f}→{after:.0f}pts)"
        else:
            pain_s = "— (no snapshot)"

        # support / resist
        sup, res = pos.get("support"), pos.get("resistance")
        if sup:
            if row_n["low"] >= sup:
                sup_s = grade("support", "HELD")
            elif row_n["close"] >= sup:
                sup_s = grade("support", "MIXED")
            else:
                sup_s = grade("support", "FAILED")
            sup_s = f"{sup_s} ({sup:.0f})"
        else:
            sup_s = "—"
        if res:
            if row_n["high"] <= res:
                res_s = grade("resist", "HELD")
            elif row_n["close"] <= res:
                res_s = grade("resist", "MIXED")
            else:
                res_s = grade("resist", "FAILED")
            res_s = f"{res_s} ({res:.0f})"
        else:
            res_s = "—"

        print(f"{d.date()!s:10s} {src:14s} {regime:8s} {pain_s:22s} {sup_s:18s} {res_s:18s}")

    print(f"\n{days[-1].date()} (today's journal): graded tomorrow - PENDING\n")
    print("tallies:")
    for name, t in tally.items():
        total = sum(t.values())
        parts = ", ".join(f"{k} {v}/{total}" for k, v in sorted(t.items()))
        print(f"  {name:10s} {parts}")


if __name__ == "__main__":
    main()
