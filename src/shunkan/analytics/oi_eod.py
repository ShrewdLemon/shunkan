"""End-of-day OI and straddle from NSE F&O bhavcopies.

Three views of the same files: OI by strike with the session's change, one series' OI through its
life at a few strikes, and the ATM straddle of the nearest expiry session by session. Every number
is end of day; the intraday versions in daily.py read the snapshot store instead.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

from shunkan.analytics.daily import _max_pain
from shunkan.analytics.facts import _wall
from shunkan.data import events_calendar as cal
from shunkan.data import fo_bhav
from shunkan.data.provider import DataError

SOURCE = "NSE F&O bhavcopy (end of day)"
KINDS = ["OPTIDX", "OPTSTK"]
WIDTH = 16          # strikes each side of the ATM in the by-strike ladder
SESSIONS = 60       # straddle history
LOOKBACK = 40       # sessions searched for a series' history
FLOOR = 0.05        # a series' history starts where its strikes carry 5% of their peak OI


@lru_cache(maxsize=512)
def _cached(day: date, symbol: str, root: str | None) -> pd.DataFrame:
    return fo_bhav.load(day, symbols=[symbol], kinds=KINDS, root=root)


def _options(day: date, symbol: str, root) -> pd.DataFrame:
    """One session's options for one underlying. Bhavcopies are write-once, so a parsed day never goes stale."""
    return _cached(day, symbol, None if root is None else str(root))


def _holidays(root) -> list[date]:
    try:
        return cal.holidays(root=root, fetch=False)
    except Exception:
        return []


def _price(row) -> float:
    return float(row["close"]) if row["contracts"] > 0 else float(row["settle"])


def _lot(df) -> float | None:
    lots = df["lot"].dropna()
    lots = lots[lots > 0]
    return float(lots.mode().iloc[0]) if len(lots) else None


def _spot(df) -> float:
    u = df["underlying"].dropna()
    if u.empty:
        raise DataError("no underlying price in the bhavcopy")
    return float(u.median())


def _sides(f) -> tuple[pd.DataFrame, pd.DataFrame, list[float]]:
    ce = f[f["right"] == "CE"].set_index("strike").sort_index()
    pe = f[f["right"] == "PE"].set_index("strike").sort_index()
    ks = sorted(set(ce.index) & set(pe.index))
    return ce.loc[ks], pe.loc[ks], ks


def _leg(side: pd.DataFrame, k: float, div: float) -> tuple[float, float, float | None, bool]:
    if k not in side.index:
        return 0.0, 0.0, None, False
    r = side.loc[k]
    return float(r["oi"]) / div, float(r["oi_change"]) / div, _price(r), bool(r["contracts"] > 0)


def strike_key(k) -> str:
    """The JSON key for a strike, spelled the way JavaScript's String() spells the number."""
    k = float(k)
    return str(int(k)) if k.is_integer() else repr(k)


def _grid(strikes) -> float:
    d = np.round(np.diff(np.unique(np.asarray(strikes, float))), 2)
    counts = Counter(d[d > 0].tolist())
    return min(counts, key=lambda s: (-counts[s], s)) if counts else 0.0


def by_strike(df: pd.DataFrame, expiry: pd.Timestamp, width: int = WIDTH) -> dict:
    f = df[df["expiry"] == expiry]
    if f.empty:
        sym = df["symbol"].iloc[0] if len(df) else "the"
        raise DataError(f"no {sym} contracts expiring {expiry.date()}")
    lot = _lot(f)
    div, spot = lot or 1.0, _spot(f)
    ce = f[f["right"] == "CE"].set_index("strike").sort_index()
    pe = f[f["right"] == "PE"].set_index("strike").sort_index()
    ks = sorted(set(ce.index) | set(pe.index))
    arr = np.asarray(ks, float)
    coi = ce["oi"].reindex(arr, fill_value=0)
    poi = pe["oi"].reindex(arr, fill_value=0)
    call_wall, call_note = _wall(coi.to_frame("oi"), spot, "CE")
    put_wall, put_note = _wall(poi.to_frame("oi"), spot, "PE")
    atm = int(np.argmin(np.abs(arr - spot)))
    rows = []
    for k in ks[max(0, atm - width): atm + width + 1]:
        c, p = _leg(ce, k, div), _leg(pe, k, div)
        rows.append({"strike": k, "call_oi": c[0], "call_oi_change": c[1], "call_price": c[2], "call_traded": c[3],
                     "put_oi": p[0], "put_oi_change": p[1], "put_price": p[2], "put_traded": p[3]})
    c_oi = float(ce["oi"].sum())
    out = {"expiry": expiry.date().isoformat(), "spot": spot, "lot": lot, "unit": "contracts" if lot else "units",
           "pcr": float(pe["oi"].sum()) / c_oi if c_oi > 0 else None,
           "max_pain": _max_pain(arr, coi.to_numpy(float), poi.to_numpy(float)),
           "call_wall": call_wall, "put_wall": put_wall, "atm_strike": ks[atm],
           "totals": {"call_oi": c_oi / div, "put_oi": float(pe["oi"].sum()) / div,
                      "call_oi_change": float(ce["oi_change"].sum()) / div,
                      "put_oi_change": float(pe["oi_change"].sum()) / div},
           "rows": rows}
    if call_note:
        out["call_wall_note"] = call_note
    if put_note:
        out["put_wall_note"] = put_note
    return out


def pick_strikes(strikes, spot: float, below: int = 2, above: int = 3) -> list[float]:
    """Strikes on twice the listing grid around the ATM; the nearest listed ones when those are missing."""
    have = sorted(set(float(k) for k in strikes))
    n = below + above + 1
    step = 2 * _grid(have)
    if step > 0:
        atm = round(spot / step) * step
        want = [atm + i * step for i in range(-below, above + 1)]
        if all(k in have for k in want):
            return want
    return sorted(sorted(have, key=lambda k: (abs(k - spot), k))[:n])


def series_oi(frames, expiry: pd.Timestamp, strikes) -> dict:
    """OI at each strike through the series' life, from the session before its OI became material.

    Counted in the latest session's lot: NSE revises lots mid-series, and dividing each session by its
    own lot would make the line jump on a day the shares barely moved."""
    days, spot, lot = [], [], None
    oi = {strike_key(k): {"call": [], "put": []} for k in strikes}
    for d, df in frames:
        f = df[df["expiry"] == expiry]
        if f.empty:
            continue
        lot = _lot(f) or lot
        days.append(d.isoformat())
        spot.append(_spot(f))
        for k in strikes:
            for right, key in (("CE", "call"), ("PE", "put")):
                v = f[(f["strike"] == k) & (f["right"] == right)]["oi"]
                oi[strike_key(k)][key].append(float(v.iloc[0]) if len(v) else None)
    if not days:
        raise DataError(f"no session in the last {len(frames)} lists the {expiry.date()} series")
    div = lot or 1.0
    oi = {k: {s: [None if x is None else x / div for x in xs] for s, xs in v.items()} for k, v in oi.items()}
    sums = [sum(oi[strike_key(k)][s][i] or 0 for k in strikes for s in ("call", "put")) for i in range(len(days))]
    peak = max(sums)
    start = max(0, next(i for i, s in enumerate(sums) if s >= FLOOR * peak) - 1) if peak > 0 else 0
    seen_first = days[0] == frames[0][0].isoformat()
    return {"expiry": expiry.date().isoformat(), "unit": "contracts" if lot else "units", "lot": lot,
            "listed": None if seen_first else days[0], "listed_before": days[0] if seen_first else None,
            "strikes": list(strikes), "days": days[start:], "spot": spot[start:],
            "oi": {k: {s: v[s][start:] for s in v} for k, v in oi.items()}}


def straddles(frames, holidays=()) -> list[dict]:
    """The ATM straddle of the nearest expiry after each session, at that session's close."""
    hols = np.array(sorted(holidays), dtype="datetime64[D]")
    out = []
    for d, df in frames:
        x = df[df["expiry"] > pd.Timestamp(d)]
        if x.empty:
            continue
        e = x["expiry"].min()
        f = x[x["expiry"] == e]
        ce, pe, ks = _sides(f)
        if not ks:
            continue
        spot = _spot(f)
        k = min(ks, key=lambda q: (abs(q - spot), q))
        c, p = ce.loc[k], pe.loc[k]
        price = _price(c) + _price(p)
        left = int(np.busday_count(d + timedelta(days=1), e.date() + timedelta(days=1), holidays=hols))
        out.append({"date": d.isoformat(), "spot": spot, "strike": k, "expiry": e.date().isoformat(),
                    "straddle": price, "implied_move_pct": price / spot * 100, "sessions_to_expiry": left,
                    "traded": bool(c["contracts"] > 0 and p["contracts"] > 0)})
    return out


def _guard(fn) -> dict:
    try:
        return fn()
    except Exception as exc:
        return {"error": str(exc) or type(exc).__name__}


def _frames(days, symbol, root) -> list[tuple[date, pd.DataFrame]]:
    def one(d):
        try:
            return d, _options(d, symbol, root)
        except DataError:
            return d, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        return [(d, df) for d, df in pool.map(one, days) if df is not None and len(df)]


def view(symbol: str, expiry: str | None = None, root=None, sessions: int = SESSIONS) -> dict:
    symbol = symbol.upper()
    days = fo_bhav.sessions(root=root)
    if not days:
        return {"symbol": symbol, "source": SOURCE, "error": "no F&O bhavcopy on disk"}
    last = days[-1]
    today = _options(last, symbol, root)
    if today.empty:
        return {"symbol": symbol, "source": SOURCE, "error": f"no {symbol} options in the {last} bhavcopy"}
    expiries = sorted(e for e in today["expiry"].unique() if e > pd.Timestamp(last))
    if not expiries:
        return {"symbol": symbol, "source": SOURCE, "error": f"no {symbol} expiry after {last}"}
    try:
        want = pd.Timestamp(expiry) if expiry else None
    except (TypeError, ValueError):
        want = None
    exp = want if want in expiries else expiries[0]
    frames = _frames(days[-max(sessions, LOOKBACK):], symbol, root)
    out = {"symbol": symbol, "as_of": last.isoformat(), "source": SOURCE,
           "expiries": [e.date().isoformat() for e in expiries], "expiry": exp.date().isoformat()}
    out["by_strike"] = _guard(lambda: by_strike(today, exp))
    out["history"] = _guard(lambda: series_oi(
        frames[-LOOKBACK:], exp, pick_strikes(today[today["expiry"] == exp]["strike"].unique(), _spot(today))))
    out["straddle"] = _guard(lambda: {"rows": straddles(frames[-sessions:], _holidays(root))})
    return out
