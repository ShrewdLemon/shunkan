"""Participant-wise derivatives positioning from NSE's public archive.

This is the table behind every "FII bearish, Client bullish" line in Indian
market commentary: NSE publishes, every evening, the open interest of four
participant classes (Client, DII, FII, Pro) split across index and stock
futures and options, at
https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv

Two properties make it the best-behaved dataset in this whole codebase:

It is backfillable. The archive serves past dates, so unlike option candles
(which vanish when a contract delists) this can be rebuilt years back with no
survivorship problem.

It is positioning, not price. Who is long and who is short is exactly the
"who is on the other side" question every edge hypothesis has to answer, and
until now we were guessing at it.

Direction convention for the derived nets: long calls and short puts are
bullish exposure; short calls and long puts are bearish. That is a delta-sign
proxy, not a delta-weighted one - the file has contract counts, not deltas,
and pretending otherwise would be manufacturing precision. The columns are
kept raw alongside the nets so nothing is hidden behind the convention.
"""

from __future__ import annotations

import io
import time
from datetime import date, timedelta

import pandas as pd

from shunkan.data.provider import DataError

ARCHIVE_URL = ("https://nsearchives.nseindia.com/content/nsccl/"
               "fao_participant_oi_{d:%d%m%Y}.csv")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/csv,*/*",
}

# NSE's column names, stripped of their inconsistent trailing spaces, mapped to
# stable snake_case keys the rest of the codebase can rely on.
_COLUMNS = {
    "Client Type": "client_type",
    "Future Index Long": "fut_idx_long",
    "Future Index Short": "fut_idx_short",
    "Future Stock Long": "fut_stk_long",
    "Future Stock Short": "fut_stk_short",
    "Option Index Call Long": "opt_idx_call_long",
    "Option Index Put Long": "opt_idx_put_long",
    "Option Index Call Short": "opt_idx_call_short",
    "Option Index Put Short": "opt_idx_put_short",
    "Option Stock Call Long": "opt_stk_call_long",
    "Option Stock Put Long": "opt_stk_put_long",
    "Option Stock Call Short": "opt_stk_call_short",
    "Option Stock Put Short": "opt_stk_put_short",
    "Total Long Contracts": "total_long",
    "Total Short Contracts": "total_short",
}


def store_path(root=None):
    from shunkan.store.store import STORE_DIR

    d = (root or STORE_DIR) / "participant"
    d.mkdir(parents=True, exist_ok=True)
    return d / "fao_oi.parquet"


def parse_participant_csv(text: str, day: date) -> pd.DataFrame:
    """Parse NSE's CSV. Pure function, unit-testable offline.

    The file opens with a quoted title line, then headers with trailing
    spaces, then Client/DII/FII/Pro/TOTAL rows. TOTAL is dropped: it is an
    aggregate of the others and keeping it doubles every sum downstream.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_i = next((i for i, ln in enumerate(lines) if ln.startswith("Client Type")), None)
    if header_i is None:
        raise DataError("participant CSV has no 'Client Type' header - format changed?")
    df = pd.read_csv(io.StringIO("\n".join(lines[header_i:])))
    df.columns = [c.strip() for c in df.columns]
    missing = set(_COLUMNS) - set(df.columns)
    if missing:
        raise DataError(f"participant CSV missing columns: {sorted(missing)}")
    df = df.rename(columns=_COLUMNS)
    df["client_type"] = df["client_type"].str.strip()
    df = df[df["client_type"].str.upper() != "TOTAL"].copy()
    for c in _COLUMNS.values():
        if c != "client_type":
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")
    df.insert(0, "date", pd.Timestamp(day))

    # Derived nets, delta-SIGN convention (see module docstring).
    df["idx_fut_net"] = df.fut_idx_long - df.fut_idx_short
    df["idx_opt_net"] = ((df.opt_idx_call_long + df.opt_idx_put_short)
                         - (df.opt_idx_call_short + df.opt_idx_put_long))
    return df


def fetch_participant_oi(day: date) -> pd.DataFrame:
    """One day's table from the archive. Raises DataError when unpublished,
    which is every weekend and holiday and is not an error worth logging."""
    import httpx

    url = ARCHIVE_URL.format(d=day)
    try:
        r = httpx.get(url, headers=_HEADERS, timeout=15.0)
    except Exception as exc:
        raise DataError(f"participant archive unreachable: {exc}") from exc
    if r.status_code != 200 or b"Client Type" not in r.content:
        raise DataError(f"no participant file for {day} (HTTP {r.status_code})")
    return parse_participant_csv(r.text, day)


def backfill(days: int = 380, root=None, progress=None) -> dict:
    """Walk back day by day, fetching whatever is missing. Idempotent.

    The archive is static files, so this is polite at ~3 requests/second and
    a year costs about two minutes. 404s on weekends and holidays are expected
    and counted separately from real failures.
    """
    path = store_path(root)
    have: set = set()
    if path.exists():
        try:
            have = set(pd.read_parquet(path, columns=["date"])["date"]
                       .dt.date.unique())
        except Exception:
            have = set()

    got, missing, failed = 0, 0, 0
    frames = []
    today = date.today()
    for i in range(days):
        d = today - timedelta(days=i)
        if d.weekday() >= 5 or d in have:
            continue
        try:
            frames.append(fetch_participant_oi(d))
            got += 1
        except DataError as exc:
            if "no participant file" in str(exc):
                missing += 1        # holiday, or today before publication
            else:
                failed += 1
        time.sleep(0.3)
        if progress and (got + missing) % 25 == 0:
            progress(f"  participant backfill: {got} fetched, {missing} unpublished")

    if frames:
        fresh = pd.concat(frames, ignore_index=True)
        if path.exists():
            try:
                fresh = pd.concat([pd.read_parquet(path), fresh], ignore_index=True)
            except Exception:
                pass
        fresh = (fresh.drop_duplicates(subset=["date", "client_type"], keep="last")
                      .sort_values(["date", "client_type"]))
        fresh.to_parquet(path, index=False)
    return {"fetched": got, "unpublished": missing, "failed": failed,
            "days_on_disk": len(have) + got}


def latest_with_change(root=None) -> dict | None:
    """The most recent day's positioning plus the change from the prior day.

    The CHANGE is what commentary actually reads: FII being net short index
    futures is a permanent structural fact (they hedge), so the level alone
    says almost nothing. Returns None when fewer than two days are on disk,
    because a change needs two observations and inventing one is not an option.
    """
    path = store_path(root)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
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
        row = {
            "idx_fut_net": int(c.idx_fut_net),
            "idx_opt_net": int(c.idx_opt_net),
        }
        if who in prev.index:
            p = prev.loc[who]
            row["idx_fut_net_chg"] = int(c.idx_fut_net - p.idx_fut_net)
            row["idx_opt_net_chg"] = int(c.idx_opt_net - p.idx_opt_net)
            # A read a human can scan, derived from the day's CHANGE in net
            # direction-sign exposure across futures and index options.
            total_chg = row["idx_fut_net_chg"] + row["idx_opt_net_chg"]
            row["read"] = ("added bullish exposure" if total_chg > 0
                           else "added bearish exposure" if total_chg < 0
                           else "unchanged")
        out["by_participant"][who] = row
    return out
