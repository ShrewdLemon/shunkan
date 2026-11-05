"""Pick the option chain to record beside a paper session."""

from __future__ import annotations

import datetime
import math

import pandas as pd

NIFTY_50 = 256265
INDIA_VIX = 264969
STEP = 50


def select_chain(
    instruments: pd.DataFrame,
    center: float,
    n: int,
    today: datetime.date,
    name: str = "NIFTY",
) -> list[int]:
    """Nearest-expiry CE+PE tokens at ±n strikes around `center`, plus NIFTY 50 and India VIX.

    `instruments` is a Kite NFO dump (`kite_fno.load_instruments`). A strike is
    kept only when both rights are listed. Raises ValueError unless `center` is a positive price.
    """
    if center is None or not math.isfinite(center) or center <= 0:
        raise ValueError(f"chain centre {center!r} is not a positive price")
    df = instruments
    expiry = pd.to_datetime(df["expiry"], errors="coerce").dt.date
    ok = (
        (df["name"] == name)
        & df["instrument_type"].isin(["CE", "PE"])
        & (df["segment"] == "NFO-OPT")
        & (expiry >= today)
    )
    out = [NIFTY_50, INDIA_VIX]
    if ok.any():
        nearest = expiry[ok].min()
        rows = df[ok & (expiry == nearest)]
        atm = round(center / STEP) * STEP
        for k in range(-n, n + 1):
            at = rows[(rows["strike"] - (atm + STEP * k)).abs() < 1e-6]
            if set(at["instrument_type"]) >= {"CE", "PE"}:
                out += [int(t) for t in at["instrument_token"]]
    return sorted(set(out))
