"""Typed parquet copy of the F&O bhavcopies, one file per month.

The raw zips stay the source of truth. build() parses each month once into
<store>/fo_bhav/typed/{YYYY}/{YYYY-MM}.parquet, rows ordered by kind, symbol,
date and then the bhavcopy sort, so one symbol sits in a few row groups. The
manifest records the raw file (name, size, mtime) behind every day and the
parquet file behind every month; a day whose raw file changed, failed to parse
or is newer than the store, or whose month file is not the one recorded, is
read from raw. The format hash covers the running parser's code and the pandas
and pyarrow versions, so a change to either retires the store.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from shunkan.data import fo_bhav
from shunkan.data.provider import DataError

ROW_GROUP = 65_536
UPDATE_MAX = 2  # months an evening update may rebuild; more means a full build is due
_TEXT = ("kind", "symbol", "right")
_MONTH_KEYS = ["kind", "symbol"]
_ARROW = {"datetime64[ns]": pa.timestamp("ns"), object: pa.string(),
          "float64": pa.float64(), "int64": pa.int64()}
_SCHEMA = pa.schema([(c, _ARROW[fo_bhav._DTYPES[c]]) for c in fo_bhav.COLUMNS])


def typed_dir(root=None) -> Path:
    return fo_bhav.raw_dir(root).parent / "typed"


def _root(root) -> Path:
    return fo_bhav.raw_dir(root).parents[1]


def _month_file(base: Path, key: str) -> Path:
    return base / key[:4] / f"{key}.parquet"


def _canon(x, doc=None) -> str:
    """Stable text for a code object (pass the function's __doc__ so editing it changes nothing)."""
    if hasattr(x, "co_code"):
        consts = x.co_consts
        if doc is not None and consts and consts[0] == doc:
            consts = consts[1:]
        return (f"code({x.co_code.hex()}|{x.co_names!r}|{x.co_varnames!r}|"
                + ",".join(_canon(k) for k in consts) + ")")
    if isinstance(x, frozenset):
        return "fs(" + ",".join(sorted(_canon(k) for k in x)) + ")"
    if isinstance(x, tuple):
        return "t(" + ",".join(_canon(k) for k in x) + ")"
    return repr(x)


def _format() -> str:
    spec = json.dumps([fo_bhav.COLUMNS, fo_bhav._DTYPES, fo_bhav._LEGACY, fo_bhav._UDIFF,
                       fo_bhav._UDIFF_OPTIONAL,
                       fo_bhav._KINDS, fo_bhav._SORT, _MONTH_KEYS, ROW_GROUP,
                       pd.__version__, pa.__version__, list(sys.version_info[:2])],
                      sort_keys=True, default=str)
    code = [_canon(fn.__code__, fn.__doc__) for fn in (fo_bhav._frame, fo_bhav.parse, fo_bhav._day, _month)]
    return hashlib.sha256("\n".join([spec, *code]).encode("utf-8")).hexdigest()[:16]


def format_hash() -> str:
    return _FORMAT


def _raw(root, only: set[date] | None = None) -> dict[date, list]:
    """day -> [name, size, mtime_ns] of the file path_for() would read."""
    base = fo_bhav.raw_dir(root)
    out: dict[date, list] = {}
    years = None if only is None else {f"{d:%Y}" for d in only}
    try:
        folders = [e for e in os.scandir(base) if e.is_dir() and (years is None or e.name in years)]
    except OSError:
        return out
    for year in folders:
        try:
            entries = list(os.scandir(year.path))
        except OSError:
            continue
        for e in entries:
            try:
                d = fo_bhav._day_of(e.name)
            except ValueError:
                continue
            if d is None or year.name != f"{d:%Y}" or (only is not None and d not in only):
                continue
            if (d in out and not e.name.startswith("BhavCopy_")) or not e.is_file():
                continue
            st = e.stat()
            out[d] = [e.name, st.st_size, st.st_mtime_ns]
    return out


def _entry_ok(e) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("src"), dict)
            and isinstance(e.get("failed"), dict) and isinstance(e.get("rows"), int)
            and all(isinstance(v, list) and len(v) == 3 for v in e["src"].values())
            and (e.get("file") is None or (isinstance(e["file"], list) and len(e["file"]) == 2)))


def _manifest(root) -> dict:
    try:
        m = json.loads((typed_dir(root) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(m, dict) or m.get("format") != format_hash() or not isinstance(m.get("months"), dict):
        return {}
    return {k: e for k, e in m["months"].items() if _entry_ok(e)}


def _durable(tmp: Path, path: Path, write) -> None:
    with open(tmp, "wb") as fh:
        write(fh)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _write_manifest(base: Path, months: dict) -> None:
    body = json.dumps({"format": format_hash(), "months": months}, sort_keys=True).encode("utf-8")
    _durable(base / "manifest.json.tmp", base / "manifest.json", lambda fh: fh.write(body))


def _stat(path: Path) -> list | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def _valid(days, root, months: dict | None = None) -> tuple[dict[str, list[date]], list[date]]:
    months = _manifest(root) if months is None else months
    if not months:
        return {}, list(days)
    base = typed_dir(root)
    raw = _raw(root, set(days))
    hit: dict[str, list[date]] = {}
    miss: list[date] = []
    files: dict[str, bool] = {}
    for d in days:
        key, iso = f"{d:%Y-%m}", d.isoformat()
        ent = months.get(key)
        rec = ent["src"].get(iso) if ent else None
        ok = rec is not None and rec == raw.get(d) and iso not in ent["failed"]
        if ok and key not in files:
            files[key] = ent.get("file") is not None and _stat(_month_file(base, key)) == ent["file"]
        if ok and files[key]:
            hit.setdefault(key, []).append(d)
        else:
            miss.append(d)
    return hit, miss


def _keep(rg, idx, lo, hi, symbols, kinds) -> bool:
    def span(col):
        s = rg.column(idx[col]).statistics
        return (s.min, s.max) if s is not None and s.has_min_max else None

    r = span("date")
    if r and (pd.Timestamp(r[1]) < lo or pd.Timestamp(r[0]) > hi):
        return False
    for col, want in (("symbol", symbols), ("kind", kinds)):
        r = span(col)
        if want is not None and r and not any(r[0] <= w <= r[1] for w in want):
            return False
    return True


def _read_month(path: Path, days: list[date], symbols, kinds) -> pa.Table:
    pf = pq.ParquetFile(path, read_dictionary=list(_TEXT))
    md = pf.metadata
    idx = {md.schema.column(i).name: i for i in range(md.num_columns)}
    lo, hi = pd.Timestamp(min(days)), pd.Timestamp(max(days))
    rgs = [i for i in range(md.num_row_groups) if _keep(md.row_group(i), idx, lo, hi, symbols, kinds)]
    tab = pf.read_row_groups(rgs) if rgs else pf.schema_arrow.empty_table()
    want = pa.array(pd.to_datetime(days).values.astype("datetime64[ns]")).cast(tab.schema.field("date").type)
    mask = pc.is_in(tab["date"], value_set=want)
    if symbols is not None:
        mask = pc.and_(mask, pc.is_in(tab["symbol"], value_set=pa.array(symbols, pa.string())))
    if kinds is not None:
        mask = pc.and_(mask, pc.is_in(tab["kind"], value_set=pa.array(kinds, pa.string())))
    return tab.filter(mask)


def _text(col: pa.ChunkedArray) -> np.ndarray:
    """str objects with None for null, as the raw parser leaves them (pandas 3 would give str/NaN)."""
    parts = []
    for ch in col.chunks:
        if not pa.types.is_dictionary(ch.type):
            ch = pc.dictionary_encode(ch)
        values = np.array(ch.dictionary.to_pylist() + [None], dtype=object)
        parts.append(values[ch.indices.fill_null(-1).to_numpy(zero_copy_only=False)])
    return np.concatenate(parts) if parts else np.empty(0, dtype=object)


def _frame(tab: pa.Table) -> pd.DataFrame:
    num = tab.select([c for c in fo_bhav.COLUMNS if c not in _TEXT]).to_pandas(split_blocks=True)
    text = {c: _text(tab[c]) for c in _TEXT}
    when = num["date"].to_numpy().astype("datetime64[ns]").view("i8")
    if len(when) > 1 and (np.diff(when) < 0).any():
        order = np.argsort(when, kind="stable")
        num = num.take(order).reset_index(drop=True)
        text = {c: v[order] for c, v in text.items()}
    else:
        num = num.copy()  # Arrow buffers are read-only; raw frames take in-place edits
    df = pd.DataFrame({c: pd.Series(text[c], dtype=object, copy=False) if c in _TEXT else num[c]
                       for c in fo_bhav.COLUMNS}, copy=False)
    return df.astype(fo_bhav._DTYPES)


def read(days, symbols=None, kinds=None, root=None) -> tuple[pd.DataFrame | None, list[date]]:
    """Rows for the stored days, in load_range order, and the days to read from raw."""
    hit, miss = _valid(days, root)
    if not hit:
        return None, list(days)
    base = typed_dir(root)
    keys = sorted(hit)

    def one(key):
        try:
            return _read_month(_month_file(base, key), hit[key], symbols, kinds)
        except (OSError, pa.ArrowException):
            return None

    if len(keys) == 1:
        tables = [one(keys[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(keys))) as pool:
            tables = list(pool.map(one, keys))
    got = [t for t in tables if t is not None]
    for key, t in zip(keys, tables):
        if t is None:
            miss.extend(hit[key])
    if not got:
        return None, sorted(miss)
    return _frame(pa.concat_tables(got)), sorted(miss)


def _month(root: Path, key: str, src: dict[str, list]) -> dict:
    base = typed_dir(root)
    frames, failed = [], {}
    for iso, (name, _size, _mtime) in sorted(src.items()):
        d = date.fromisoformat(iso)
        try:
            frames.append(fo_bhav._day(d, fo_bhav.raw_dir(root) / iso[:4] / name))
        except DataError as exc:
            if isinstance(exc.__cause__, (OSError, MemoryError)):
                raise
            failed[iso] = str(exc)
    path = _month_file(base, key)
    if not frames:
        path.unlink(missing_ok=True)
        return {"src": src, "failed": failed, "rows": 0, "file": None}
    full = pd.concat(frames, ignore_index=True).sort_values(_MONTH_KEYS, kind="stable", ignore_index=True)
    tab = pa.Table.from_pandas(full, schema=_SCHEMA, preserve_index=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    _durable(path.with_suffix(".tmp"), path,
             lambda fh: pq.write_table(tab, fh, row_group_size=ROW_GROUP, compression="zstd"))
    return {"src": src, "failed": failed, "rows": len(full), "file": _stat(path)}


@contextmanager
def _lock(base: Path):
    base.mkdir(parents=True, exist_ok=True)
    with open(base / ".lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _fresh_file(base: Path, key: str, ent: dict) -> bool:
    if not ent["rows"]:
        return True
    path = _month_file(base, key)
    if _stat(path) != ent.get("file"):
        return False
    try:
        return pq.ParquetFile(path).metadata.num_rows == ent["rows"]
    except (OSError, pa.ArrowException):
        return False


def build(root=None, workers: int | None = None, create: bool = True) -> dict:
    """Bring the store level with the raw files.

    create=False is the evening update: it never starts a store and rebuilds at
    most UPDATE_MAX months; anything bigger waits for `shunkan fo-store`.
    """
    t0 = time.perf_counter()
    root = _root(root)
    base = typed_dir(root)
    if not create and not _manifest(root):
        return {"skipped": "no typed store yet; run `shunkan fo-store`"}
    with _lock(base) as ok:
        if not ok:
            return {"busy": True}
        months = _manifest(root)
        want: dict[str, dict[str, list]] = {}
        for d, rec in _raw(root).items():
            want.setdefault(f"{d:%Y-%m}", {})[d.isoformat()] = rec
        todo = [k for k in sorted(want) if k not in months
                or months[k]["src"] != want[k] or not _fresh_file(base, k, months[k])]
        if not create and len(todo) > UPDATE_MAX:
            return {"skipped": f"{len(todo)} months out of date; run `shunkan fo-store`"}
        for key in [k for k in months if k not in want]:
            _month_file(base, key).unlink(missing_ok=True)
            del months[key]
        n = workers if workers is not None else max(1, min(6, (os.cpu_count() or 2) - 2))
        err = None
        if n > 1 and len(todo) > 1:
            with ProcessPoolExecutor(max_workers=n) as pool:
                futs = {pool.submit(_month, root, k, want[k]): k for k in todo}
                for f in as_completed(futs):
                    try:
                        months[futs[f]] = f.result()
                    except Exception as exc:
                        err = err or exc
                        continue
                    _write_manifest(base, months)
        else:
            for k in todo:
                months[k] = _month(root, k, want[k])
                _write_manifest(base, months)
        _write_manifest(base, months)
        if err is not None:
            raise err
    out = summary(root)
    out.update(built=len(todo), seconds=round(time.perf_counter() - t0, 2))
    return out


def summary(root=None) -> dict:
    months = _manifest(root)
    base = typed_dir(root)
    failed = {d: msg for ent in months.values() for d, msg in ent["failed"].items()}
    stored = sum(len(ent["src"]) for ent in months.values()) - len(failed)
    size = sum(p.stat().st_size for p in base.glob("*/*.parquet")) if base.is_dir() else 0
    return {"months": len(months), "days": stored, "failed": failed,
            "rows": sum(ent["rows"] for ent in months.values()), "bytes": size}


_FORMAT = _format()
