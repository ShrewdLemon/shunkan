"""Risk-free rate for option maths: the RBI policy repo rate in force on a day.

repo_rate.csv (packaged) lists every change since 2018-08-01 with the RBI release
it came from. IV barely depends on the choice: a 100 bp error moves a 30-day ATM
IV by about 0.01 vol points. A day after the last row carries the last rate; add
a row when the MPC moves.
"""

from __future__ import annotations

import io
from functools import lru_cache
from importlib import resources

import numpy as np
import pandas as pd

FILE = "repo_rate.csv"


@lru_cache(maxsize=1)
def table() -> tuple[np.ndarray, np.ndarray]:
    """(effective days as datetime64[D], rates as decimals), strictly increasing in day."""
    text = resources.files("shunkan.data").joinpath(FILE).read_text(encoding="utf-8")
    df = pd.read_csv(io.StringIO(text), comment="#")
    days = pd.to_datetime(df["effective_date"], format="%Y-%m-%d").to_numpy().astype("datetime64[D]")
    pct = df["repo_pct"].astype(float).to_numpy()
    if len(days) == 0 or (np.diff(days.astype("int64")) <= 0).any() or not ((pct > 0) & (pct < 20)).all():
        raise ValueError(f"{FILE} must list increasing dates with rates in (0, 20)%")
    return days, pct / 100.0


def repo_on(days) -> np.ndarray:
    d = np.asarray(days).astype("datetime64[D]")
    eff, r = table()
    i = np.searchsorted(eff, d, side="right") - 1
    if np.size(i) and (np.asarray(i) < 0).any():
        raise ValueError(f"no repo rate before {eff[0]}")
    return r[i]
