"""Implied vol and greeks for every traded NSE option at the close, one file per month.

build() derives <store>/fo_bhav/iv/{YYYY}/{YYYY-MM}.parquet from the typed store
(fo_store). Per (date, symbol, expiry) the forward is the median of K + (C - P)/DF
over the PAIRS strikes where both sides traded and the call and put closes are
closest. One such strike alone is a stale print often enough (its forward misses
the future by 3% at the 99th percentile, against 0.3% for three) that the close of
the same-expiry future wins over it when one traded. DF = exp(-r T), r the RBI repo
rate in force that day (rates.py), T calendar days / 365 to the expiry (NSE's actual
expiry when a corporate action ends a contract early). Only rows that
traded get an IV, and `code` (black76.CODES) says why one has none. A stored month
is served while the typed month behind it and the rates for its days are unchanged;
any other day is computed the same way from fo_bhav. Greeks are computed on read.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from shunkan.data import fo_bhav, fo_store, rates
from shunkan.derivatives import black76

PAIRS = 3
UPDATE_MAX = fo_store.UPDATE_MAX
OPTIONS = ("OPTIDX", "OPTSTK")
FUTURES = ("FUTIDX", "FUTSTK")
FWD_PARITY, FWD_FUTURE, FWD_NONE = 0, 1, -1
COLUMNS = ["date", "kind", "symbol", "expiry", "strike", "right", "close", "contracts", "oi",
           "forward", "fwd_src", "pairs", "t", "df", "iv", "code"]
GREEKS = ["delta", "gamma", "vega", "theta"]
_DTYPES = {"date": "datetime64[ns]", "kind": object, "symbol": object,
           "expiry": "datetime64[ns]", "strike": "float64", "right": object,
           "close": "float64", "contracts": "int64", "oi": "int64", "forward": "float64",
           "fwd_src": "int8", "pairs": "int8", "t": "float64", "df": "float64",
           "iv": "float64", "code": "int8"}
_ARROW = {"datetime64[ns]": pa.timestamp("ns"), object: pa.string(), "float64": pa.float64(),
          "int64": pa.int64(), "int8": pa.int8()}
_SCHEMA = pa.schema([(c, _ARROW[_DTYPES[c]]) for c in COLUMNS])
_TEXT = ("kind", "symbol", "right")
_GROUP = ["date", "symbol", "expiry"]
_STORE_ORDER = ["kind", "symbol", "date", "expiry", "strike", "right"]


def iv_dir(root=None) -> Path:
    return fo_bhav.raw_dir(root).parent / "iv"


def _years(day: pd.Series, expiry: pd.Series) -> np.ndarray:
    gap = expiry.to_numpy().astype("datetime64[D]") - day.to_numpy().astype("datetime64[D]")
    return gap.astype("int64") / 365.0


def _ends(rows: pd.DataFrame) -> pd.Series:
    """The day a contract really expires: NSE's actual expiry when it comes earlier."""
    early = rows["expiry_actual"].notna() & (rows["expiry_actual"] < rows["expiry"])
    return rows["expiry"].where(~early, rows["expiry_actual"])


def _discount(day: pd.Series, t: np.ndarray) -> np.ndarray:
    return np.exp(-rates.repo_on(day.to_numpy()) * t)


def forwards(opts: pd.DataFrame, futs: pd.DataFrame) -> pd.DataFrame:
    """One row per (date, symbol, expiry) in opts: forward, fwd_src, pairs."""
    groups = opts[_GROUP].drop_duplicates(ignore_index=True)
    on = _GROUP + ["strike"]
    pr = opts.loc[opts["right"] == "CE", on + ["close", "expiry_actual"]].merge(
        opts.loc[opts["right"] == "PE", on + ["close"]], on=on, suffixes=("_c", "_p"))
    diff = (pr["close_c"] - pr["close_p"]).to_numpy()
    df = _discount(pr["date"], _years(pr["date"], _ends(pr)))
    pr = pr.assign(gap=np.abs(diff), fk=pr["strike"].to_numpy() + diff / df)
    pr = pr.sort_values(_GROUP + ["gap", "strike"], kind="stable")
    top = pr[pr.groupby(_GROUP, sort=False).cumcount() < PAIRS]
    fw = top.groupby(_GROUP, sort=False).agg(forward=("fk", "median"), pairs=("fk", "size")).reset_index()
    fw = fw[fw["forward"] > 0]
    fut = (futs.sort_values(_GROUP, kind="stable").drop_duplicates(_GROUP)
           .rename(columns={"close": "fut"})[_GROUP + ["fut"]])
    out = groups.merge(fw, on=_GROUP, how="left").merge(fut, on=_GROUP, how="left")
    pairs = out["pairs"].fillna(0).to_numpy()
    has_fut = (out["fut"] > 0).to_numpy()
    parity = (pairs >= 2) | ((pairs == 1) & ~has_fut)
    future = ~parity & has_fut
    out["forward"] = np.where(parity, out["forward"], np.where(future, out["fut"], np.nan))
    out["fwd_src"] = np.where(parity, FWD_PARITY, np.where(future, FWD_FUTURE, FWD_NONE)).astype("int8")
    out["pairs"] = np.where(parity, pairs, 0).astype("int8")
    return out[_GROUP + ["forward", "fwd_src", "pairs"]]


def compute(frame: pd.DataFrame) -> pd.DataFrame:
    """IV rows (COLUMNS) for the traded options in a fo_bhav frame, in store order."""
    close = frame["close"].to_numpy()
    live = (frame["contracts"].to_numpy() > 0) & np.isfinite(close) & (close > 0)
    keep = ["date", "kind", "symbol", "expiry", "strike", "right", "close", "contracts", "oi",
            "expiry_actual"]
    opts = frame.loc[live & frame["kind"].isin(OPTIONS).to_numpy()
                     & frame["right"].isin(("CE", "PE")).to_numpy()
                     & (frame["strike"].to_numpy() > 0), keep]
    futs = frame.loc[live & frame["kind"].isin(FUTURES).to_numpy(), _GROUP + ["close"]]
    if opts.empty:
        return pd.DataFrame({c: pd.Series(dtype=t) for c, t in _DTYPES.items()})
    o = opts.merge(forwards(opts, futs), on=_GROUP, how="left")
    t = _years(o["date"], _ends(o))
    df = _discount(o["date"], t)
    iv, code = black76.implied_vol(o["close"].to_numpy(), o["forward"].to_numpy(),
                                   o["strike"].to_numpy(), t, df, (o["right"] == "CE").to_numpy())
    o = o.assign(t=t, df=df, iv=iv, code=code)
    return o.sort_values(_STORE_ORDER, kind="stable", ignore_index=True)[COLUMNS].astype(_DTYPES)


def with_greeks(rows: pd.DataFrame) -> pd.DataFrame:
    ok = (rows["code"] == black76.OK).to_numpy()
    g = black76.greeks(rows["forward"].to_numpy()[ok], rows["strike"].to_numpy()[ok],
                       rows["t"].to_numpy()[ok], rows["df"].to_numpy()[ok],
                       rows["iv"].to_numpy()[ok], (rows["right"] == "CE").to_numpy()[ok])
    out = rows.copy()
    for name in GREEKS:
        col = np.full(len(rows), np.nan)
        col[ok] = g[name]
        out[name] = col
    return out


def _format() -> str:
    spec = json.dumps([COLUMNS, _DTYPES, _STORE_ORDER, _GROUP, _TEXT, PAIRS, OPTIONS, FUTURES,
                       [FWD_PARITY, FWD_FUTURE, FWD_NONE], black76.CODES,
                       [black76.VOL_LO, black76.VOL_HI], list(black76._C), black76._INV_SQRT_2PI,
                       black76.implied_vol.__defaults__, fo_store.ROW_GROUP, pd.__version__,
                       pa.__version__, np.__version__, list(sys.version_info[:2])],
                      sort_keys=True, default=str)
    fns = (forwards, compute, _years, _ends, _discount, _month, rates.repo_on,
           black76.implied_vol, black76.price, black76._erfc, black76.ncdf, black76.npdf,
           black76._d, black76._arrays)
    code = [fo_store._canon(fn.__code__, fn.__doc__) for fn in fns]
    return hashlib.sha256("\n".join([spec, *code]).encode("utf-8")).hexdigest()[:16]


def format_hash() -> str:
    return _FORMAT


def _rate_mark(isos) -> str:
    days = sorted(isos)
    r = rates.repo_on(np.array(days, dtype="datetime64[D]")) if days else np.empty(0)
    body = json.dumps([[d, repr(float(x))] for d, x in zip(days, r)])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _entry_ok(e) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("days"), list)
            and isinstance(e.get("rows"), int) and isinstance(e.get("rate"), str)
            and isinstance(e.get("typed"), list) and len(e["typed"]) == 2
            and isinstance(e.get("codes"), dict)
            and (e.get("file") is None or (isinstance(e["file"], list) and len(e["file"]) == 2)))


def _manifest(root) -> dict:
    try:
        m = json.loads((iv_dir(root) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(m, dict) or m.get("format") != format_hash() or not isinstance(m.get("months"), dict):
        return {}
    return {k: e for k, e in m["months"].items() if _entry_ok(e)}


def _write_manifest(base: Path, months: dict) -> None:
    body = json.dumps({"format": format_hash(), "months": months}, sort_keys=True).encode("utf-8")
    fo_store._durable(base / "manifest.json.tmp", base / "manifest.json", lambda fh: fh.write(body))


def _typed_id(tent: dict | None) -> list | None:
    return None if tent is None else [tent.get("file"), tent["rows"]]


def _vouched(tent: dict) -> list[str]:
    return sorted(d for d in tent["src"] if d not in tent["failed"])


def _current(ent: dict | None, tent: dict | None) -> bool:
    return (ent is not None and tent is not None and ent["typed"] == _typed_id(tent)
            and ent["days"] == _vouched(tent) and ent["rate"] == _rate_mark(ent["days"]))


def _valid(days, root) -> tuple[dict[str, list[date]], list[date]]:
    ivm = _manifest(root)
    if not ivm:
        return {}, list(days)
    typed = fo_store._manifest(root)
    t_hit, _ = fo_store._valid(days, root, typed)
    base = iv_dir(root)
    hit: dict[str, list[date]] = {}
    miss: list[date] = []
    state: dict[str, str] = {}
    for d in days:
        key = f"{d:%Y-%m}"
        if key not in state:
            ent = ivm.get(key)
            if not _current(ent, typed.get(key)):
                state[key] = "stale"
            elif ent["rows"] == 0 and ent.get("file") is None:
                state[key] = "empty"
            elif fo_store._stat(fo_store._month_file(base, key)) == ent["file"]:
                state[key] = "file"
            else:
                state[key] = "stale"
        if state[key] != "stale" and d in t_hit.get(key, ()):
            if state[key] == "file":
                hit.setdefault(key, []).append(d)
        else:
            miss.append(d)
    return hit, miss


def _read_month(path: Path, days: list[date], symbols, kinds) -> pa.Table:
    pf = pq.ParquetFile(path, read_dictionary=list(_TEXT))
    md = pf.metadata
    idx = {md.schema.column(i).name: i for i in range(md.num_columns)}
    lo, hi = pd.Timestamp(min(days)), pd.Timestamp(max(days))
    rgs = [i for i in range(md.num_row_groups)
           if fo_store._keep(md.row_group(i), idx, lo, hi, symbols, kinds)]
    tab = pf.read_row_groups(rgs) if rgs else pf.schema_arrow.empty_table()
    want = pa.array(pd.to_datetime(days).values.astype("datetime64[ns]")).cast(tab.schema.field("date").type)
    mask = pc.is_in(tab["date"], value_set=want)
    if symbols is not None:
        mask = pc.and_(mask, pc.is_in(tab["symbol"], value_set=pa.array(symbols, pa.string())))
    if kinds is not None:
        mask = pc.and_(mask, pc.is_in(tab["kind"], value_set=pa.array(kinds, pa.string())))
    return tab.filter(mask)


def _frame(tab: pa.Table) -> pd.DataFrame:
    num = tab.select([c for c in COLUMNS if c not in _TEXT]).to_pandas(split_blocks=True).copy()
    text = {c: fo_store._text(tab[c]) for c in _TEXT}
    return pd.DataFrame({c: pd.Series(text[c], dtype=object, copy=False) if c in _TEXT else num[c]
                         for c in COLUMNS}, copy=False).astype(_DTYPES)


def _by_date(rows: pd.DataFrame) -> pd.DataFrame:
    """Store order -> load order (date, kind, symbol, expiry, strike, right)."""
    when = rows["date"].to_numpy().astype("datetime64[ns]").view("i8")
    if len(when) > 1 and (np.diff(when) < 0).any():
        return rows.take(np.argsort(when, kind="stable")).reset_index(drop=True)
    return rows.reset_index(drop=True)


def _computed(days: list[date], symbols, root) -> pd.DataFrame:
    kinds = list(OPTIONS + FUTURES)
    got, rest = fo_store.read(days, symbols, kinds, root)
    frames = [] if got is None else [got]
    frames += [fo_bhav._load_raw(d, symbols, kinds, root) for d in rest]
    if not frames:
        return compute(pd.DataFrame({c: pd.Series(dtype=t) for c, t in fo_bhav._DTYPES.items()}))
    return compute(pd.concat(frames, ignore_index=True))


def read(days, symbols=None, kinds=None, root=None) -> pd.DataFrame:
    """IV rows for `days` in load order: stored months where current, computed otherwise."""
    hit, miss = _valid(days, root)
    base = iv_dir(root)
    keys = sorted(hit)

    def one(key):
        try:
            return _read_month(fo_store._month_file(base, key), hit[key], symbols, kinds)
        except (OSError, pa.ArrowException):
            return None

    if len(keys) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(keys))) as pool:
            tables = list(pool.map(one, keys))
    else:
        tables = [one(k) for k in keys]
    parts = []
    stored = [t for t in tables if t is not None]
    if stored:
        parts.append(_frame(pa.concat_tables(stored)))
    for key, t in zip(keys, tables):
        if t is None:
            miss.extend(hit[key])
    if miss:
        extra = _computed(sorted(miss), symbols, root)
        if kinds is not None:
            extra = extra[extra["kind"].isin(kinds)]
        parts.append(extra)
    if not parts:
        return compute(pd.DataFrame({c: pd.Series(dtype=t) for c, t in fo_bhav._DTYPES.items()}))
    return _by_date(pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0])


def _kinds(kinds):
    if kinds is None:
        return None
    kinds = [str(k).upper() for k in kinds]
    bad = [k for k in kinds if k not in OPTIONS]
    if bad:
        raise ValueError(f"IV rows exist for {OPTIONS} only, not {bad}")
    return kinds


def load(day, symbols=None, kinds=None, root=None, greeks: bool = True) -> pd.DataFrame:
    return load_range(day, day, symbols, kinds, root, greeks)


def load_range(start, end, symbols=None, kinds=None, root=None, greeks: bool = True) -> pd.DataFrame:
    """Traded option rows from start to end with forward, T, DF, IV, code and greeks."""
    symbols = fo_bhav._names(symbols, "symbols")
    kinds = _kinds(kinds)
    days = fo_bhav.sessions(start, end, root)
    rows = read(days, symbols, kinds, root)
    return with_greeks(rows) if greeks else rows


def _month(root: Path, key: str) -> dict:
    tent = fo_store._manifest(root).get(key)
    base = iv_dir(root)
    path = fo_store._month_file(base, key)
    if tent is None or not tent["rows"]:
        path.unlink(missing_ok=True)
        return {"typed": _typed_id(tent) or [None, 0], "days": [], "rate": _rate_mark([]),
                "rows": 0, "file": None, "codes": {}}
    isos = _vouched(tent)
    days = [date.fromisoformat(d) for d in isos]
    got, rest = fo_store.read(days, None, list(OPTIONS + FUTURES), root)
    if rest or got is None:
        raise RuntimeError(f"typed month {key} changed under the IV build")
    rows = compute(got)
    if rows.empty:
        path.unlink(missing_ok=True)
        return {"typed": _typed_id(tent), "days": isos, "rate": _rate_mark(isos),
                "rows": 0, "file": None, "codes": {}}
    tab = pa.Table.from_pandas(rows, schema=_SCHEMA, preserve_index=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fo_store._durable(path.with_suffix(".tmp"), path,
                      lambda fh: pq.write_table(tab, fh, row_group_size=fo_store.ROW_GROUP,
                                                compression="zstd"))
    codes = {str(int(c)): int(n) for c, n in zip(*np.unique(rows["code"].to_numpy(), return_counts=True))}
    return {"typed": _typed_id(tent), "days": isos, "rate": _rate_mark(isos),
            "rows": len(rows), "file": fo_store._stat(path), "codes": codes}


def _fresh_file(base: Path, key: str, ent: dict) -> bool:
    if not ent["rows"]:
        return ent.get("file") is None
    path = fo_store._month_file(base, key)
    if fo_store._stat(path) != ent.get("file"):
        return False
    try:
        return pq.ParquetFile(path).metadata.num_rows == ent["rows"]
    except (OSError, pa.ArrowException):
        return False


def build(root=None, workers: int | None = None, create: bool = True) -> dict:
    """Bring the IV months level with the typed store (build that first).

    create=False is the evening update: it never starts a store and rebuilds at
    most UPDATE_MAX months.
    """
    t0 = time.perf_counter()
    root = fo_store._root(root)
    base = iv_dir(root)
    if not create and not _manifest(root):
        return {"skipped": "no IV store yet; run `shunkan fo-store`"}
    with fo_store._lock(base) as ok:
        if not ok:
            return {"busy": True}
        typed = fo_store._manifest(root)
        if not typed:
            return {"skipped": "typed store missing or unreadable; run `shunkan fo-store`"}
        months = _manifest(root)
        todo = [k for k in sorted(typed) if not (_current(months.get(k), typed[k])
                                                  and _fresh_file(base, k, months[k]))]
        if not create and len(todo) > UPDATE_MAX:
            return {"skipped": f"{len(todo)} IV months out of date; run `shunkan fo-store`"}
        for key in [k for k in months if k not in typed]:
            fo_store._month_file(base, key).unlink(missing_ok=True)
            del months[key]
        n = workers if workers is not None else max(1, min(6, (os.cpu_count() or 2) - 2))
        err = None
        if n > 1 and len(todo) > 1:
            with ProcessPoolExecutor(max_workers=n) as pool:
                futs = {pool.submit(_month, root, k): k for k in todo}
                for f in as_completed(futs):
                    try:
                        months[futs[f]] = f.result()
                    except Exception as exc:
                        err = err or exc
                        continue
                    _write_manifest(base, months)
        else:
            for k in todo:
                months[k] = _month(root, k)
                _write_manifest(base, months)
        _write_manifest(base, months)
        if err is not None:
            raise err
    out = summary(root)
    out.update(built=len(todo), seconds=round(time.perf_counter() - t0, 2))
    return out


def summary(root=None) -> dict:
    months = _manifest(root)
    base = iv_dir(root)
    codes: dict[str, int] = {}
    for ent in months.values():
        for c, n in ent["codes"].items():
            codes[black76.CODES[int(c)]] = codes.get(black76.CODES[int(c)], 0) + n
    size = sum(p.stat().st_size for p in base.glob("*/*.parquet")) if base.is_dir() else 0
    return {"months": len(months), "days": sum(len(e["days"]) for e in months.values()),
            "rows": sum(e["rows"] for e in months.values()), "codes": codes, "bytes": size}


_FORMAT = _format()
