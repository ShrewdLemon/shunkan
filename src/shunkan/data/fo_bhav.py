"""NSE F&O daily bhavcopies on disk: readers never write; fetch() is the only raw writer.

One zip per session under <store>/fo_bhav/raw/{YYYY}/: the legacy form to
2024-07-05, UDiFF from 2024-07-08, and an empty {YYYYMMDD}.missing where NSE
published nothing. Both forms parse to one frame (COLUMNS). contracts counts
contracts, oi counts units; contracts == 0 means the row did not trade.
load() and load_range() read the typed copy (fo_store) where it is current.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from shunkan.data import nse_archive
from shunkan.data.provider import DataError, FetchFailed

INDEX_SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")
COLUMNS = ["date", "kind", "symbol", "expiry", "strike", "right",
           "open", "high", "low", "close", "settle",
           "contracts", "oi", "oi_change", "underlying", "lot"]
_DTYPES = {"date": "datetime64[ns]", "kind": object, "symbol": object,
           "expiry": "datetime64[ns]", "strike": "float64", "right": object,
           "open": "float64", "high": "float64", "low": "float64",
           "close": "float64", "settle": "float64", "contracts": "int64",
           "oi": "int64", "oi_change": "int64", "underlying": "float64",
           "lot": "float64"}

_LEGACY = {"TIMESTAMP": "date", "INSTRUMENT": "kind", "SYMBOL": "symbol",
           "EXPIRY_DT": "expiry", "STRIKE_PR": "strike", "OPTION_TYP": "right",
           "OPEN": "open", "HIGH": "high", "LOW": "low", "CLOSE": "close",
           "SETTLE_PR": "settle", "CONTRACTS": "contracts", "OPEN_INT": "oi",
           "CHG_IN_OI": "oi_change"}
_UDIFF = {"TradDt": "date", "FinInstrmTp": "kind", "TckrSymb": "symbol",
          "XpryDt": "expiry", "StrkPric": "strike", "OptnTp": "right",
          "OpnPric": "open", "HghPric": "high", "LwPric": "low", "ClsPric": "close",
          "SttlmPric": "settle", "TtlTradgVol": "contracts", "OpnIntrst": "oi",
          "ChngInOpnIntrst": "oi_change", "UndrlygPric": "underlying",
          "NewBrdLotQty": "lot"}
_KINDS = {"IDO": "OPTIDX", "IDF": "FUTIDX", "STO": "OPTSTK", "STF": "FUTSTK"}
_MON = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
        "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_LEGACY_NAME = re.compile(r"^fo(\d{2})([A-Z]{3})(\d{4})bhav\.csv\.zip$")
_UDIFF_NAME = re.compile(r"^BhavCopy_NSE_FO_0_0_0_(\d{8})_F_0000\.csv\.zip$")
_SORT = ["kind", "symbol", "expiry", "strike", "right"]
_UDIFF_FROM = date(2024, 7, 8)


def raw_dir(root=None) -> Path:
    if root is None:
        from shunkan.store.store import STORE_DIR

        root = STORE_DIR
    return Path(root) / "fo_bhav" / "raw"


def _as_date(day) -> date:
    return pd.Timestamp(day).date()


def _day_of(name: str) -> date | None:
    m = _UDIFF_NAME.match(name)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    m = _LEGACY_NAME.match(name)
    if m and m.group(2) in _MON:
        return date(int(m.group(3)), _MON.index(m.group(2)) + 1, int(m.group(1)))
    return None


def path_for(day, root=None) -> Path | None:
    d = _as_date(day)
    folder = raw_dir(root) / f"{d:%Y}"
    for name in (f"BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip",
                 f"fo{d:%d}{_MON[d.month - 1]}{d:%Y}bhav.csv.zip"):
        if (folder / name).is_file():
            return folder / name
    return None


def url_for(day) -> str:
    d = _as_date(day)
    if d >= _UDIFF_FROM:
        return f"{nse_archive.ARCHIVE}/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    mon = _MON[d.month - 1]
    return (f"{nse_archive.ARCHIVE}/content/historical/DERIVATIVES/{d:%Y}/{mon}/"
            f"fo{d:%d}{mon}{d:%Y}bhav.csv.zip")


def fetch(day, root=None, client=None) -> Path:
    d = _as_date(day)
    have = path_for(d, root)
    if have is not None:
        return have
    url = url_for(d)
    name = url.rsplit("/", 1)[-1]
    data = nse_archive.get(url, "zip", client=client)
    try:
        df = _frame(pd.read_csv(io.BytesIO(data), compression="zip", dtype=str,
                                keep_default_na=False))
    except Exception as exc:
        raise FetchFailed(f"{name}: {exc}") from exc
    if not (df["date"] == pd.Timestamp(d)).all():
        raise FetchFailed(f"{name} carries {sorted(set(df['date'].dt.date))}, not {d}")
    return nse_archive.save_once(raw_dir(root) / f"{d:%Y}" / name, data)


def status(day, root=None) -> str:
    d = _as_date(day)
    if path_for(d, root):
        return "file"
    if (raw_dir(root) / f"{d:%Y}" / f"{d:%Y%m%d}.missing").exists():
        return "missing"
    return "absent"


def sessions(start=None, end=None, root=None) -> list[date]:
    base = raw_dir(root)
    if not base.is_dir():
        return []
    lo = _as_date(start) if start is not None else None
    hi = _as_date(end) if end is not None else None
    days = {d for p in base.glob("*/*.zip") if (d := _day_of(p.name))
            and (lo is None or d >= lo) and (hi is None or d <= hi)}
    return sorted(days)


def parse(path) -> pd.DataFrame:
    path = Path(path)
    try:
        raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception as exc:
        raise DataError(f"unreadable bhavcopy {path.name}: {exc}") from exc
    try:
        return _frame(raw)
    except Exception as exc:
        raise DataError(f"{path.name}: {exc}") from exc


def _frame(raw: pd.DataFrame) -> pd.DataFrame:
    raw.columns = [c.strip() for c in raw.columns]
    if "TradDt" in raw.columns:
        cols, fmt = _UDIFF, "%Y-%m-%d"
    elif "INSTRUMENT" in raw.columns:
        cols, fmt = _LEGACY, "%d-%b-%Y"
    else:
        raise ValueError("neither legacy nor UDiFF columns")
    missing = set(cols) - set(raw.columns)
    if missing:
        raise ValueError(f"missing columns {sorted(missing)}")
    if raw.empty:
        raise ValueError("header but no rows")
    df = raw[list(cols)].rename(columns=cols)
    for c in ("underlying", "lot"):
        if c not in df:
            df[c] = ""
    df["kind"] = df["kind"].str.strip().replace(_KINDS)
    df["symbol"] = df["symbol"].str.strip()
    for c in ("date", "expiry"):
        df[c] = pd.to_datetime(df[c].str.strip(), format=fmt)
    right = df["right"].str.strip().astype(object)
    df["right"] = right.where(right.isin(["CE", "PE"]), None)
    for c in ("strike", "open", "high", "low", "close", "settle", "underlying", "lot"):
        df[c] = pd.to_numeric(df[c].str.strip().replace("", None))
    df["strike"] = df["strike"].fillna(0.0)
    for c in ("contracts", "oi", "oi_change"):
        df[c] = pd.to_numeric(df[c].str.strip().replace("", "0"))
    return df[COLUMNS].astype(_DTYPES).reset_index(drop=True)


def _names(arg, what) -> list[str] | None:
    if arg is None:
        return None
    if isinstance(arg, str):
        raise TypeError(f"{what} must be a list of names, not the str {arg!r}")
    return [a.upper() for a in arg]


def _day(d: date, p: Path) -> pd.DataFrame:
    df = parse(p)
    if not (df["date"] == pd.Timestamp(d)).all():
        raise DataError(f"{p.name} carries {sorted(set(df['date'].dt.date))}, not {d}")
    return df.sort_values(_SORT, na_position="first").reset_index(drop=True)


def _load_raw(d: date, symbols, kinds, root) -> pd.DataFrame:
    p = path_for(d, root)
    if p is None:
        raise DataError(f"no F&O bhavcopy on disk for {d} ({status(d, root)})")
    df = _day(d, p)
    if symbols is not None:
        df = df[df["symbol"].isin(symbols)]
    if kinds is not None:
        df = df[df["kind"].isin(kinds)]
    return df.reset_index(drop=True)


def load(day, symbols=None, kinds=None, root=None) -> pd.DataFrame:
    from shunkan.data import fo_store

    symbols, kinds = _names(symbols, "symbols"), _names(kinds, "kinds")
    d = _as_date(day)
    got, rest = fo_store.read([d], symbols, kinds, root)
    return _load_raw(d, symbols, kinds, root) if rest else got


def load_range(start, end, symbols=None, kinds=None, root=None) -> pd.DataFrame:
    """Typed store first; days it does not hold come from the raw zips."""
    from shunkan.data import fo_store

    symbols, kinds = _names(symbols, "symbols"), _names(kinds, "kinds")
    days = sessions(start, end, root)
    if not days:
        return pd.DataFrame(columns=COLUMNS).astype(_DTYPES)
    got, rest = fo_store.read(days, symbols, kinds, root)
    frames = [_load_raw(d, symbols, kinds, root) for d in rest]
    if got is None:
        return pd.concat(frames, ignore_index=True)
    if not frames:
        return got
    out = pd.concat([got, *frames], ignore_index=True)
    order = np.argsort(out["date"].to_numpy().view("i8"), kind="stable")
    return out.take(order).reset_index(drop=True)
