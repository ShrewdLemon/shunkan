"""The news archive: headlines persisted through time, mapped to companies.

Until now news existed only in the moment it was fetched, which made every
"did the stock react to the news" question unanswerable - there was nothing to
join a move against. This module gives headlines the same treatment as chains
and participant data: append-only parquet under the store, with real source
timestamps, so event-reaction studies become possible.

Two channels feed it:

LIVE: the server loop persists the market feed and a rotating slice of
constituent queries every half hour. This is the unbiased channel - whatever
the feed said at the time, kept.

BACKFILL: Google News RSS honours `after:`/`before:` date operators, so
history can be pulled per company per week. Verified before building: a March
2026 window returned 100 items with pubDates inside the window.

WHAT THE BACKFILL IS NOT, stated here because the harvest taught us to say it
up front: a retrospective query returns what Google indexes TODAY, ranked by
Google, capped at ~100 items per window. Articles get delisted, ranking is not
chronology, and coverage intensity is therefore NOT measurable from this
channel. It is adequate for "was there named news about this company around
day X", which is what an event join needs, and rows carry origin="backfill" so
research can always separate the two channels. Timestamps on older items are
date-granular (08:00 GMT), which is fine for daily joins and useless for
intraday ones.

Mapping is by company name IN THE TITLE (see constituents.map_title), never by
query attribution alone. A headline can map to several companies; it is stored
once with all of them.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from shunkan.data.constituents import alias_table, map_title, universe
from shunkan.data.provider import DataError

_BACKFILL_DELAY = 0.9   # polite to Google; a year of one symbol is ~1 minute


def news_dir(root=None):
    from shunkan.store.store import STORE_DIR

    d = (root or STORE_DIR) / "news"
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_file(origin: str = "live", root=None):
    """One file PER WRITER, and that is the whole design.

    The first version used a single headlines.parquet, and two legitimate
    writers (the host backfill script and the container's half-hour loop) did
    read-modify-write against it concurrently. When one read landed mid-write
    it saw an unreadable file, and the old recovery path - "rewrite rather
    than lose the fetch" - kept only its own rows and clobbered 10,084 rows
    down to 675. The recovery path caused the loss.

    So: the live loop writes live.parquet, the container's rotation writes
    rotation.parquet, the host backfill writes backfill.parquet. Writers never
    share a file, there is nothing to lock across a macOS host and a Linux
    container (where flock over a bind mount is not trustworthy), and readers
    merge everything and dedup. The legacy single file, where it survives, is
    read-only.
    """
    safe = re.sub(r"[^a-z0-9_-]", "", origin.lower()) or "live"
    return news_dir(root) / f"{safe}.parquet"


def _all_files(root=None):
    d = news_dir(root)
    legacy = d / "headlines.parquet"
    files = sorted(f for f in d.glob("*.parquet")
                   if not f.name.endswith(".corrupt.parquet"))
    return ([legacy] if legacy.exists() and legacy not in files else []) + files


def _read_all(root=None) -> pd.DataFrame:
    frames = []
    for f in _all_files(root):
        try:
            frames.append(pd.read_parquet(f))
        except Exception:
            continue  # a bad file never poisons the merge; persist quarantines it
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates(subset=["key"], keep="first")


def _key(title: str, published) -> str:
    """Dedup key: normalised title + date.

    Not the URL: Google News links carry per-fetch redirect tokens, so the
    same article gets a different URL every time it is fetched and a URL key
    would store every article dozens of times.
    """
    t = re.sub(r"[^a-z0-9]", "", (title or "").lower())[:120]
    d = published.date().isoformat() if published is not None else "nodate"
    return f"{t}|{d}"


def persist(items, origin: str, aliases=None, root=None,
            query_symbol: str | None = None) -> int:
    """Write NewsItems into the archive. Idempotent. Returns rows added.

    `query_symbol` records which company the QUERY was about; the title map
    decides which companies the row is tagged with. When they disagree the
    title wins, because the query channel is how listicles arrive.
    """
    from shunkan.intel.sentiment import score_sentiment

    if not items:
        return 0
    if aliases is None:
        aliases = alias_table(universe())
    path = store_file(origin, root)
    have: set = set()
    frames = []
    if path.exists():
        try:
            old = pd.read_parquet(path)
            have = set(old["key"])
            frames.append(old)
        except Exception:
            # NEVER overwrite what cannot be read. Quarantine the bytes for
            # forensics and start a fresh file; the old fallback proceeded
            # with an empty frame and clobbered 10,084 rows down to 675.
            path.rename(path.with_suffix(f".{int(time.time())}.corrupt.parquet"))
    # dedup against EVERY writer's file, not only our own, so the same
    # article fetched through two channels is still stored once
    for f in _all_files(root):
        if f == path:
            continue
        try:
            have |= set(pd.read_parquet(f, columns=["key"])["key"])
        except Exception:
            continue

    rows = []
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for it in items:
        k = _key(it.title, it.published)
        if k in have:
            continue
        have.add(k)
        symbols = map_title(it.title, aliases)
        rows.append({
            "key": k,
            "ts": (it.published.isoformat() if it.published else None),
            "title": it.title,
            "source": getattr(it, "source", "") or "",
            "symbols": " ".join(symbols),
            "query_symbol": query_symbol or "",
            "sentiment": float(score_sentiment(it.title)),
            "origin": origin,
            "fetched_at": fetched_at,
        })
    if not rows:
        return 0
    frames.append(pd.DataFrame(rows))
    out = pd.concat(frames, ignore_index=True)
    out.to_parquet(path, index=False)
    return len(rows)


def backfill_symbol(symbol: str, name: str, weeks: int = 52,
                    end: date | None = None, aliases=None, root=None,
                    progress=None, origin: str = "backfill") -> dict:
    """Pull weekly windows of history for one company, newest first.

    Query is the company NAME in quotes - that is what headlines say - and
    every returned title still has to pass the title map to be tagged, so a
    week of noise costs storage, not correctness.
    """
    from shunkan.intel.feeds import fetch_news

    end = end or date.today()
    added = fetched = failed = 0
    for w in range(weeks):
        hi = end - timedelta(days=7 * w)
        lo = hi - timedelta(days=7)
        q = f'"{name}" after:{lo:%Y-%m-%d} before:{hi:%Y-%m-%d}'
        try:
            items = fetch_news(q, limit=100)
            fetched += len(items)
            added += persist(items, origin=origin, aliases=aliases,
                             root=root, query_symbol=symbol)
        except Exception:
            failed += 1
        time.sleep(_BACKFILL_DELAY)
        if progress and (w + 1) % 13 == 0:
            progress(f"  {symbol}: {w + 1}/{weeks} weeks, +{added} rows")
    return {"symbol": symbol, "weeks": weeks, "fetched": fetched,
            "added": added, "failed_windows": failed}


def news_for(symbol: str, days: int = 3650, root=None) -> pd.DataFrame:
    """Archived headlines tagged with this symbol, oldest first."""
    df = _read_all(root)
    if df.empty:
        return df
    sym = symbol.upper()
    pat = r"(?:^|\s)" + re.escape(sym) + r"(?:\s|$)"
    df = df[df["symbols"].str.contains(pat, regex=True, na=False)].copy()
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    return df[df["ts"] >= cutoff].sort_values("ts")


def coverage(root=None) -> pd.DataFrame:
    """Rows per symbol per origin, so a gap is visible rather than assumed."""
    df = _read_all(root)
    if df.empty:
        return df
    rows = []
    for _, r in df.iterrows():
        for s in (r["symbols"].split() or ["(untagged)"]):
            rows.append({"symbol": s, "origin": r["origin"]})
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows).value_counts(["symbol", "origin"])
            .rename("headlines").reset_index()
            .pivot_table(index="symbol", columns="origin", values="headlines",
                         fill_value=0).reset_index())
