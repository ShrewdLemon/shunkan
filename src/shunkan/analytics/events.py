"""Event studies on the underlying.

Root first. Before asking what an option surface did around an event, establish
what the UNDERLYING does: how NIFTY behaves after a 2-sigma down day, how a
stock behaves after its own shock, what the base rates actually are. Thirty
years of daily closes can answer that today; the derivatives layer joins in as
the capture archive accumulates.

Three honesty rules are load-bearing here, because event studies are where
plausible-looking nonsense is easiest to manufacture:

NO LOOKAHEAD. A shock is a return standardised by TRAILING vol lagged one day.
The shock day must not contribute to the vol that classifies it, or the
biggest events get quietly reclassified as ordinary.

NO OVERLAP. After an event, later events inside the forward horizon are
dropped. Two shocks a day apart share almost their whole forward window, and
counting both turns one observation into two.

ALWAYS A BASELINE. "The market rises after X" is empty until you know it rises
after anything. Every conditional statistic ships next to the unconditional
one computed on the same series over the same horizons.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

HORIZONS = (1, 2, 3, 5, 10, 21)


@dataclass
class HorizonStat:
    n: int
    mean_pct: float
    median_pct: float
    hit_rate: float          # P(cumulative return > 0)
    t_stat: float | None     # None where windows overlap (baseline)


@dataclass
class EventStudyResult:
    symbol: str
    kind: str                # e.g. "shock_down_2.0s"
    params: dict
    n_events: int
    first: str
    last: str
    horizons: dict[int, HorizonStat]
    baseline: dict[int, HorizonStat]
    recent_events: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        def h(d):
            return {str(k): vars(v) for k, v in d.items()}
        return {
            "symbol": self.symbol, "kind": self.kind, "params": self.params,
            "n_events": self.n_events, "first": self.first, "last": self.last,
            "horizons": h(self.horizons), "baseline": h(self.baseline),
            "recent_events": self.recent_events, "note": self.note,
        }


def standardised_returns(close: pd.Series, lookback: int = 63) -> pd.Series:
    """Daily log return divided by trailing vol, vol lagged one day.

    The lag is the whole point: the day being classified must not feed the
    vol that classifies it.
    """
    r = np.log(close.astype(float)).diff()
    vol = r.rolling(lookback).std().shift(1)
    return r / vol


def shock_days(close: pd.Series, sigma: float = 2.0, direction: str = "down",
               lookback: int = 63) -> pd.DatetimeIndex:
    z = standardised_returns(close, lookback)
    if direction == "down":
        mask = z <= -sigma
    elif direction == "up":
        mask = z >= sigma
    else:
        mask = z.abs() >= sigma
    return pd.DatetimeIndex(z.index[mask.fillna(False)])


def _drop_overlapping(close_index: pd.DatetimeIndex, events: pd.DatetimeIndex,
                      refractory: int) -> list[int]:
    """Positions of events, keeping the FIRST of any cluster.

    Refractory is in trading days: an event inside another's forward window is
    not a second observation, it is the same episode continuing.
    """
    pos_of = {ts: i for i, ts in enumerate(close_index)}
    kept: list[int] = []
    last = -10**9
    for ts in events:
        i = pos_of.get(ts)
        if i is None:
            continue
        if i - last > refractory:
            kept.append(i)
            last = i
    return kept


def _forward_cum(logc: np.ndarray, starts: list[int], h: int) -> np.ndarray:
    ok = [i for i in starts if i + h < len(logc)]
    return np.array([logc[i + h] - logc[i] for i in ok], dtype=float)


def _stat(cums: np.ndarray, with_t: bool) -> HorizonStat:
    n = len(cums)
    if n == 0:
        return HorizonStat(0, float("nan"), float("nan"), float("nan"), None)
    mean = float(cums.mean()) * 100
    med = float(np.median(cums)) * 100
    hit = float((cums > 0).mean())
    t = None
    if with_t and n > 2:
        sd = cums.std(ddof=1)
        t = float(cums.mean() / (sd / math.sqrt(n))) if sd > 1e-12 else None
    return HorizonStat(n, mean, med, hit, t)


def event_study(close: pd.Series, symbol: str, sigma: float = 2.0,
                direction: str = "down", lookback: int = 63,
                horizons: tuple[int, ...] = HORIZONS) -> EventStudyResult:
    """What happens after a shock, against what happens after any day.

    Event windows are non-overlapping by construction, so the conditional
    t-stats are plain ones. The baseline uses every valid start day; its
    windows overlap, which does not bias the mean but does invalidate a
    t-stat, so the baseline carries none.
    """
    close = close.dropna().astype(float)
    close.index = pd.DatetimeIndex(close.index)
    logc = np.log(close.to_numpy())
    refractory = max(horizons)

    events = shock_days(close, sigma, direction, lookback)
    kept = _drop_overlapping(close.index, events, refractory)

    cond = {h: _stat(_forward_cum(logc, kept, h), with_t=True) for h in horizons}
    all_starts = list(range(lookback + 1, len(logc)))
    base = {h: _stat(_forward_cum(logc, all_starts, h), with_t=False) for h in horizons}

    dates = [close.index[i].date().isoformat() for i in kept]
    return EventStudyResult(
        symbol=symbol,
        kind=f"shock_{direction}_{sigma:g}s",
        params={"sigma": sigma, "direction": direction, "lookback": lookback,
                "refractory": refractory},
        n_events=len(kept),
        first=dates[0] if dates else "",
        last=dates[-1] if dates else "",
        horizons=cond,
        baseline=base,
        recent_events=dates[-5:],
        note=(f"{len(events) - len(kept)} clustered events dropped to keep "
              f"forward windows non-overlapping"),
    )


def excess_event_study(close: pd.Series, benchmark: pd.Series, symbol: str,
                       sigma: float = 2.0, direction: str = "down",
                       lookback: int = 63,
                       horizons: tuple[int, ...] = HORIZONS) -> EventStudyResult:
    """Company reaction with the index stripped out.

    Events are the STOCK's own shocks; outcomes are measured in excess log
    returns (stock minus benchmark, beta assumed 1 and stated), so a stock
    that merely fell with the market does not read as having recovered when
    the market did.
    """
    df = pd.concat({"s": close, "b": benchmark}, axis=1).dropna()
    if len(df) < lookback * 3:
        raise ValueError(f"only {len(df)} overlapping sessions; need {lookback * 3}")
    excess = (np.log(df["s"]) - np.log(df["b"])).to_numpy()
    # not in place: pandas under copy-on-write can hand back a read-only view
    excess_logc = excess - excess[0]

    events = shock_days(df["s"], sigma, direction, lookback)
    kept = _drop_overlapping(pd.DatetimeIndex(df.index), events, max(horizons))

    cond = {h: _stat(_forward_cum(excess_logc, kept, h), with_t=True) for h in horizons}
    all_starts = list(range(lookback + 1, len(excess_logc)))
    base = {h: _stat(_forward_cum(excess_logc, all_starts, h), with_t=False)
            for h in horizons}
    dates = [pd.Timestamp(df.index[i]).date().isoformat() for i in kept]
    return EventStudyResult(
        symbol=symbol,
        kind=f"excess_shock_{direction}_{sigma:g}s",
        params={"sigma": sigma, "direction": direction, "lookback": lookback,
                "refractory": max(horizons), "benchmark_beta": 1.0},
        n_events=len(kept), first=dates[0] if dates else "",
        last=dates[-1] if dates else "", horizons=cond, baseline=base,
        recent_events=dates[-5:],
        note="outcomes are excess of the benchmark at beta 1, stated not fitted",
    )


def classify_today(close: pd.Series, sigma: float = 2.0,
                   lookback: int = 63) -> dict:
    """Is the latest archived session itself an event, and how big."""
    z = standardised_returns(close, lookback)
    if z.dropna().empty:
        return {"z": None, "classification": "insufficient history"}
    last = float(z.dropna().iloc[-1])
    if last <= -sigma:
        cls = "down_shock"
    elif last >= sigma:
        cls = "up_shock"
    else:
        cls = "ordinary"
    return {"z": round(last, 2), "classification": cls,
            "as_of": pd.Timestamp(z.dropna().index[-1]).date().isoformat()}
