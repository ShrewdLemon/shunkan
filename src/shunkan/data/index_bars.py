"""Official NSE daily index bars (ind_close_all on nsearchives), fetched write-once."""

from __future__ import annotations

import io
import math
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from shunkan.data import nse_archive
from shunkan.data.provider import DataError, FetchFailed

NAMES = {"NIFTY": "Nifty 50", "BANKNIFTY": "Nifty Bank", "INDIAVIX": "India VIX"}
REQUIRED = ("Nifty 50", "Nifty Bank")
_NAME, _DATE = "Index Name", "Index Date"
_COLS = {"open": "Open Index Value", "high": "High Index Value", "low": "Low Index Value",
         "close": "Closing Index Value", "change": "Points Change"}


def _as_date(day) -> date:
    return pd.Timestamp(day).date()


def url_for(day) -> str:
    return f"{nse_archive.ARCHIVE}/content/indices/ind_close_all_{_as_date(day):%d%m%Y}.csv"


def raw_dir(root=None) -> Path:
    if root is None:
        from shunkan.store.store import STORE_DIR

        root = STORE_DIR
    return Path(root) / "indices" / "raw"


def path_for(day, root=None) -> Path:
    d = _as_date(day)
    return raw_dir(root) / f"{d:%Y}" / f"ind_close_all_{d:%d%m%Y}.csv"


def _num(v, where) -> float:
    if v in ("", "-"):
        return math.nan
    try:
        return float(v)
    except (TypeError, ValueError):
        raise DataError(f"{where} {v!r} is not a number") from None


def _read(src, name, d) -> dict[str, dict]:
    try:
        raw = pd.read_csv(src, dtype=str, keep_default_na=False)
    except Exception as exc:
        raise DataError(f"unreadable {name}: {exc}") from exc
    raw.columns = [c.strip() for c in raw.columns]
    missing = {_NAME, _DATE, *_COLS.values()} - set(raw.columns)
    if missing:
        raise DataError(f"{name}: missing columns {sorted(missing)}")
    if raw.empty:
        raise DataError(f"{name}: header but no rows")
    raw = raw.apply(lambda s: s.str.strip()).drop_duplicates()
    out = {}
    for _, r in raw.iterrows():
        idx = r[_NAME]
        if idx in out:
            raise DataError(f"{name} has differing rows for {idx}")
        try:
            day = datetime.strptime(r[_DATE], "%d-%m-%Y").date()
        except (TypeError, ValueError):
            raise DataError(f"{name}: {idx} has Index Date {r[_DATE]!r}") from None
        out[idx] = {"date": day, **{k: _num(r[c], f"{name}: {idx} {c}") for k, c in _COLS.items()}}
    dates = sorted({r["date"] for r in out.values()})
    if dates != [d]:
        raise DataError(f"{name} carries {', '.join(map(str, dates))}, not {d}")
    return out


def parse(path, day) -> dict[str, dict]:
    path = Path(path)
    return _read(path, path.name, _as_date(day))


def fetch(day, root=None, client=None) -> Path:
    d = _as_date(day)
    path = path_for(d, root)
    if path.exists():
        return path
    data = nse_archive.get(url_for(d), "csv", must_contain=b"Index Name", client=client)
    try:
        rows = _read(io.BytesIO(data), path.name, d)
    except DataError as exc:
        raise FetchFailed(str(exc)) from exc
    missing = [n for n in REQUIRED if n not in rows]
    if missing:
        raise FetchFailed(f"{path.name} has no {', '.join(missing)}")
    return nse_archive.save_once(path, data)


def bar(day, symbol, root=None) -> dict:
    d = _as_date(day)
    name = NAMES.get(symbol)
    if name is None:
        raise DataError(f"no official index bar for {symbol!r}")
    path = path_for(d, root)
    if not path.is_file():
        raise DataError(f"no ind_close_all for {d}")
    rows = parse(path, d)
    if name not in rows:
        raise DataError(f"{path.name} has no {name}")
    r = rows[name]
    o, h, lo, c = r["open"], r["high"], r["low"], r["close"]
    ok = all(math.isfinite(x) and x > 0 for x in (o, h, lo, c))
    if not ok or lo > min(o, c) or h < max(o, c):
        raise DataError(f"{name} in {path.name} is not a bar: O {o} H {h} L {lo} C {c}")
    return {"symbol": symbol, "date": d.isoformat(), "open": o, "high": h, "low": lo,
            "close": c, "change": r["change"], "source": path.name}
