"""Daily-analysis helpers: replay, journal, intraday migration, VWAP.

Born from a dogfooding session (2026-08-18): grading Monday's analysis
against Tuesday's tape took a hand-written script, because the analysis
endpoint could only speak about NOW. These helpers make yesterday a query.

Three ideas, one rule each:

REPLAY. positioning_from_snapshot() rebuilds the derivatives map from what
the capture loop actually stored on day D - never from a re-fetch, which
would be today's data wearing yesterday's date.

JOURNAL. What the terminal said at the close gets written once, refused on
overwrite, and preferred over reconstruction on replay: a record beats a
recomputation, because code changes and the record does not.

MIGRATION. T-1 expiry maps die at the open (measured: Monday's max pain
24,350 re-anchored to 24,200 by late morning and price never visited the
old level). The intraday path of pain and walls IS the object; this module
computes it from the day's snapshots.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


def _max_pain(strikes: np.ndarray, call_oi: np.ndarray, put_oi: np.ndarray) -> float | None:
    if len(strikes) == 0 or (call_oi.sum() + put_oi.sum()) <= 0:
        return None
    pain = [
        float((np.maximum(k - strikes, 0) * call_oi).sum()
              + (np.maximum(strikes - k, 0) * put_oi).sum())
        for k in strikes
    ]
    return float(strikes[int(np.argmin(pain))])


def positioning_from_snapshot(front: pd.DataFrame) -> dict:
    """The derivatives map exactly as one stored snapshot knew it.

    IVs and mids exist in snapshots from 2026-08-18; before that they are
    honest NaN and the vol fields come back None with the reason attached.
    """
    strikes = front["strike"].to_numpy(dtype=float)
    coi = front["call_oi"].to_numpy(dtype=float)
    poi = front["put_oi"].to_numpy(dtype=float)
    spot = float(front["spot"].iloc[0]) if len(front) else float("nan")
    mp = _max_pain(strikes, coi, poi)
    out = {
        "source": f"chain store snapshot {front['ts'].iloc[0]}",
        "is_model": False,
        "expiry": str(front["expiry"].iloc[0]),
        "spot": None if np.isnan(spot) else spot,
        "pcr_oi": float(poi.sum() / coi.sum()) if coi.sum() > 0 else None,
        "max_pain": mp,
        "dist_to_max_pain_pct": ((mp / spot - 1) * 100
                                 if mp and spot and not np.isnan(spot) else None),
        "support": float(strikes[int(np.argmax(poi))]) if poi.sum() > 0 else None,
        "resistance": float(strikes[int(np.argmax(coi))]) if coi.sum() > 0 else None,
    }
    # vol fields only when the snapshot era stored them
    ivs = front[["call_iv", "put_iv"]].to_numpy(dtype=float) if "call_iv" in front else None
    if ivs is None or np.isnan(ivs).all():
        out["atm_iv_pct"] = None
        out["vol_note"] = ("snapshot predates IV storage (2026-08-18); "
                          "re-solving from stale LTPs is refused by design")
    else:
        i = int(np.argmin(np.abs(strikes - spot)))
        pair = [v for v in (front["call_iv"].iloc[i], front["put_iv"].iloc[i])
                if v is not None and not np.isnan(v)]
        out["atm_iv_pct"] = float(np.mean(pair)) * 100 if pair else None
    return out


def intraday_migration(symbol: str, root=None) -> dict:
    """Today's pain/wall path from the day's snapshots, front expiry only."""
    from shunkan.store.store import ChainStore

    snaps = ChainStore(root).snapshots_today(symbol)
    if snaps is None or snaps.empty:
        return {"error": "no snapshots captured today - capture needs an open "
                         "session and a live token"}
    front_exp = snaps["expiry"].min()
    front = snaps[snaps["expiry"] == front_exp]
    times = sorted(front["ts"].unique())
    path = []
    for ts in times:
        g = front[front["ts"] == ts]
        strikes = g["strike"].to_numpy(dtype=float)
        coi = g["call_oi"].to_numpy(dtype=float)
        poi = g["put_oi"].to_numpy(dtype=float)
        spot = float(g["spot"].iloc[0])
        mp = _max_pain(strikes, coi, poi)
        if mp is None or np.isnan(spot):
            continue
        path.append({"ts": str(ts), "spot": spot, "max_pain": mp,
                     "pcr": float(poi.sum() / coi.sum()) if coi.sum() > 0 else None})
    first = front[front["ts"] == times[0]].set_index("strike")
    last = front[front["ts"] == times[-1]].set_index("strike")
    dc = (last["call_oi"] - first["call_oi"]).dropna().sort_values(ascending=False)
    dp = (last["put_oi"] - first["put_oi"]).dropna().sort_values(ascending=False)
    return {
        "expiry": str(front_exp),
        "n_snapshots": len(times),
        "window": [str(times[0]), str(times[-1])],
        "path": path,
        "walls_built": {
            "calls": [[float(k), float(v)] for k, v in dc.head(3).items()],
            "puts": [[float(k), float(v)] for k, v in dp.head(3).items()],
        },
        "note": ("wall build is measured from the day's FIRST capture, not the "
                 "open; a dead token in the morning leaves that stretch dark"),
    }


# ---- the journal -----------------------------------------------------------


def journal_dir(root=None) -> Path:
    from shunkan.store.store import STORE_DIR

    d = (root or STORE_DIR) / "analysis"
    d.mkdir(parents=True, exist_ok=True)
    return d


def journal_path(symbol: str, day: date, root=None) -> Path:
    d = journal_dir(root) / symbol.upper()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{day.isoformat()}.json"


def write_journal(symbol: str, day: date, payload: dict, root=None) -> bool:
    """Record what the terminal said. Once. A day observed at its close is
    worth more than any later recomputation, so overwrite is refused."""
    path = journal_path(symbol, day, root)
    if path.exists():
        return False
    body = dict(payload)
    body["_journal"] = {"recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "symbol": symbol.upper(), "day": day.isoformat()}
    path.write_text(json.dumps(body, default=str))
    return True


def read_journal(symbol: str, day: date, root=None) -> dict | None:
    path = journal_path(symbol, day, root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ---- participants as-of ----------------------------------------------------


def participants_asof(on: date, root=None) -> dict | None:
    """latest_with_change, but as the world stood after day `on`'s file."""
    from shunkan.data.participant import store_path

    path = store_path(root)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df = df[df["date"] <= pd.Timestamp(on)]
    days = sorted(df["date"].unique())
    if len(days) < 2:
        return None
    cur = df[df["date"] == days[-1]].set_index("client_type")
    prev = df[df["date"] == days[-2]].set_index("client_type")
    out = {"date": pd.Timestamp(days[-1]).date().isoformat(),
           "prev_date": pd.Timestamp(days[-2]).date().isoformat(),
           "by_participant": {}}
    for who in cur.index:
        c = cur.loc[who]
        row = {"idx_fut_net": int(c.idx_fut_net), "idx_opt_net": int(c.idx_opt_net)}
        if who in prev.index:
            p = prev.loc[who]
            row["idx_fut_net_chg"] = int(c.idx_fut_net - p.idx_fut_net)
            row["idx_opt_net_chg"] = int(c.idx_opt_net - p.idx_opt_net)
            total = row["idx_fut_net_chg"] + row["idx_opt_net_chg"]
            row["read"] = ("added bullish exposure" if total > 0
                           else "added bearish exposure" if total < 0 else "unchanged")
        out["by_participant"][who] = row
    return out


# ---- VWAP ------------------------------------------------------------------


def vwap_today(symbol: str, root=None) -> tuple[float | None, str]:
    """Volume-weighted average of today's locally captured 1-min bars.

    Indices print no volume, so their VWAP is refused with the reason - the
    honest read there needs the future's tape, which is not streamed yet."""
    from shunkan.store import TickStore

    df = TickStore(root=root).read_bars(symbol.upper()) if root else \
        TickStore().read_bars(symbol.upper())
    if df is None or df.empty:
        return None, "no locally captured bars today"
    vol = df["volume"].to_numpy(dtype=float)
    px = df["close"].to_numpy(dtype=float)
    if vol.sum() <= 0:
        return None, ("bars carry zero volume (index tape); VWAP needs the "
                      "future's prints, which are not streamed yet")
    return float((px * vol).sum() / vol.sum()), f"{len(df)} one-minute bars"
