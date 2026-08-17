"""Expiry-day angle: what the contracts archive can and cannot answer."""
from __future__ import annotations
import glob, math
from pathlib import Path
import numpy as np
import pandas as pd

CONTRACTS = Path.home() / ".shunkan/store/contracts"
HIST = Path.home() / ".shunkan/store/history"


def load_panel() -> pd.DataFrame:
    frames = []
    for f in sorted(CONTRACTS.glob("*_day.parquet")):
        sym = f.stem.split("_", 1)[0]
        df = pd.read_parquet(f)
        df["symbol"] = sym
        frames.append(df)
    p = pd.concat(frames, ignore_index=True)
    # ts arrives as an IST-stamped string; keep the LOCAL trading date.
    t = pd.to_datetime(p["ts"], format="mixed", utc=True)
    p["date"] = t.dt.tz_convert("Asia/Kolkata").dt.date
    p["date"] = pd.to_datetime(p["date"])
    p["expiry"] = pd.to_datetime(p["expiry"])
    return p


def spot(sym: str) -> pd.DataFrame:
    name = {"NIFTY": "_NSEI", "BANKNIFTY": "_NSEBANK"}[sym]
    s = pd.read_parquet(HIST / f"{name}.parquet")[["date", "open", "high", "low", "close"]]
    s["date"] = pd.to_datetime(s["date"])
    return s.rename(columns={c: f"spot_{c}" for c in ["open", "high", "low", "close"]})


if __name__ == "__main__":
    p = load_panel()
    print(f"rows {len(p):,}  contracts {p['tradingsymbol'].nunique():,}")
    print(f"dates {p['date'].min().date()} .. {p['date'].max().date()}")
    print()
    g = (p.groupby(["symbol", "expiry"])
           .agg(first=("date", "min"), last=("date", "max"),
                n=("close", "size"), strikes=("strike", "nunique"))
           .reset_index())
    g["expiry_d"] = g["expiry"].dt.date
    g["last_dte_seen"] = (g["expiry"] - g["last"]).dt.days
    g["expiry_observed"] = g["last"] >= g["expiry"]
    print(g[["symbol", "expiry_d", "first", "last", "n", "strikes",
             "last_dte_seen", "expiry_observed"]].to_string(index=False))
    print()
    print("EXPIRY DAYS OBSERVED ANYWHERE IN THE ARCHIVE:",
          int((p["date"] >= p["expiry"]).sum()))
    print("minimum calendar days-to-expiry ever seen:",
          int((p["expiry"] - p["date"]).dt.days.min()))
