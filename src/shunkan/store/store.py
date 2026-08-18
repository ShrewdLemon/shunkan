"""Local market-data store — the terminal's memory.

Everything Shunkan sees gets captured to parquet under ~/.shunkan/store:

    store/bars1m/SYMBOL/YYYY-MM-DD.parquet    1-minute bars built from ticks
    store/ticks/YYYY-MM-DD/HHMMSS.parquet     raw tick segments (audit trail)
    store/chains/SYMBOL/YYYY-MM-DD.parquet    option-chain snapshots (per strike)

This is what makes derived numbers REAL instead of proxies:
- ΔOI        = today's OI minus a stored earlier snapshot (timestamped basis)
- IV rank    = current ATM IV vs the distribution of locally captured days —
               refuses to report below a minimum history rather than faking it
- straddle   = ATM straddle premium across today's snapshots

Every reader returns its basis (timestamps, observation counts) so the UI
can show exactly where a number came from.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from shunkan.config import APP_DIR
from shunkan.markets import IST

STORE_DIR = APP_DIR / "store"

MIN_IV_RANK_DAYS = 20  # below this we report "insufficient history", never a number


def _day_str(d: date | None = None) -> str:
    return (d or datetime.now(tz=IST).date()).isoformat()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _write_parquet(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def _read_parquet(path: Path) -> pd.DataFrame | None:
    try:
        if path.exists():
            return pd.read_parquet(path)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Bars: built live from the tick stream
# ---------------------------------------------------------------------------


@dataclass
class Bar:
    symbol: str
    minute: int  # unix minute (ts // 60)
    open: float
    high: float
    low: float
    close: float
    volume: float  # traded volume within the bar (from cumulative day volume)


class BarBuilder:
    """Aggregates ticks into 1-minute bars per symbol.

    Tick volume from Kite quote mode is the *cumulative day* volume, so a
    bar's traded volume is cum_end - cum_start. Completed bars accumulate
    in `done` until drained by the flusher. Thread-safe via a lock (ticks
    land on the ticker thread; the flusher runs on the event loop).
    """

    def __init__(self) -> None:
        self._open: dict[str, Bar] = {}
        self._cum_at_open: dict[str, float] = {}
        self.done: list[Bar] = []
        self._lock = threading.Lock()

    def on_tick(self, symbol: str, ltp: float, cum_volume: float, ts: float | None = None) -> None:
        minute = int((ts if ts is not None else time.time()) // 60)
        with self._lock:
            bar = self._open.get(symbol)
            if bar is None or bar.minute != minute:
                if bar is not None:
                    bar.volume = max(self._last_cum(symbol, cum_volume) - self._cum_at_open.get(symbol, 0.0), 0.0)
                    self.done.append(bar)
                self._open[symbol] = Bar(symbol, minute, ltp, ltp, ltp, ltp, 0.0)
                self._cum_at_open[symbol] = cum_volume
                self._last_cum_map = getattr(self, "_last_cum_map", {})
            bar = self._open[symbol]
            bar.high = max(bar.high, ltp)
            bar.low = min(bar.low, ltp)
            bar.close = ltp
            self._set_last_cum(symbol, cum_volume)

    def _set_last_cum(self, symbol: str, v: float) -> None:
        if not hasattr(self, "_last_cum_map"):
            self._last_cum_map: dict[str, float] = {}
        self._last_cum_map[symbol] = v

    def _last_cum(self, symbol: str, fallback: float) -> float:
        return getattr(self, "_last_cum_map", {}).get(symbol, fallback)

    def drain(self) -> list[Bar]:
        with self._lock:
            out, self.done = self.done, []
        return out


class TickStore:
    """Persists bars (and optional raw tick segments) to parquet."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or STORE_DIR

    def write_bars(self, bars: list[Bar]) -> int:
        if not bars:
            return 0
        df_new = pd.DataFrame(
            [{"minute": b.minute, "open": b.open, "high": b.high, "low": b.low,
              "close": b.close, "volume": b.volume, "symbol": b.symbol} for b in bars]
        )
        n = 0
        for symbol, group in df_new.groupby("symbol"):
            path = self.root / "bars1m" / str(symbol) / f"{_day_str()}.parquet"
            existing = _read_parquet(path)
            merged = (
                pd.concat([existing, group]).drop_duplicates(subset="minute", keep="last")
                if existing is not None else group
            )
            _write_parquet(path, merged.sort_values("minute"))
            n += len(group)
        return n

    def write_tick_segment(self, rows: list[dict]) -> None:
        if not rows:
            return
        ts = datetime.now(tz=IST).strftime("%H%M%S")
        path = self.root / "ticks" / _day_str() / f"{ts}.parquet"
        _write_parquet(path, pd.DataFrame(rows))

    def read_bars(self, symbol: str, day: date | None = None) -> pd.DataFrame | None:
        return _read_parquet(self.root / "bars1m" / symbol.upper() / f"{_day_str(day)}.parquet")


# ---------------------------------------------------------------------------
# Chain snapshots
# ---------------------------------------------------------------------------


class ChainStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or STORE_DIR

    def _path(self, symbol: str, day: date | None = None) -> Path:
        return self.root / "chains" / symbol.upper() / f"{_day_str(day)}.parquet"

    def snapshot(self, chain) -> None:
        """Append one snapshot (a row per strike) to today's file.

        Model chains are never stored — the store holds only real
        observations, otherwise every derived number becomes a fake. The
        `is_model` flag is the contract; the source strings stay behind it
        as a belt-and-braces check for duck-typed callers.
        """
        if (getattr(chain, "is_model", True)
                or "synthetic" in chain.source.lower()
                or "model" in chain.source.lower()):
            return
        ts = _now_utc().isoformat(timespec="seconds")
        n = len(chain.strikes)
        df_new = pd.DataFrame({
            "ts": [ts] * n,
            "expiry": [str(chain.expiry)] * n,
            "spot": [chain.spot] * n,
            "strike": chain.strikes,
            "call_ltp": chain.call_ltp, "call_oi": chain.call_oi,
            "call_iv": chain.call_iv, "call_volume": chain.call_volume,
            "put_ltp": chain.put_ltp, "put_oi": chain.put_oi,
            "put_iv": chain.put_iv, "put_volume": chain.put_volume,
            # Mids make the stored IVs re-derivable; older files lack these
            # columns and concat fills them NaN, which readers must expect.
            "call_mid": (chain.call_mid if getattr(chain, "call_mid", None)
                         is not None else [np.nan] * n),
            "put_mid": (chain.put_mid if getattr(chain, "put_mid", None)
                        is not None else [np.nan] * n),
            "source": [chain.source] * n,
        })
        path = self._path(chain.symbol)
        existing = _read_parquet(path)
        if existing is not None:
            # Dedup on (ts, expiry), not ts alone. Two expiries captured in the
            # same wall-clock second are two different observations, and the old
            # check dropped the second: the archive would look like it had term
            # structure while holding one expiry repeated. That is exactly the
            # kind of silently-wrong data this codebase refuses to store.
            dup = ((existing["ts"] == ts) & (existing["expiry"] == str(chain.expiry)))
            if bool(dup.any()):
                return
            df_new = pd.concat([existing, df_new])
        _write_parquet(path, df_new)

    def write_day_snapshot(self, symbol: str, day: date, df: pd.DataFrame) -> None:
        """Write one back-dated snapshot, for seeding a ΔOI basis from the
        exchange's own settled history.

        Refuses to overwrite: a day we actually observed is worth more than a
        reconstruction of it, and silently replacing one with the other would
        make the basis label a lie.
        """
        path = self._path(symbol, day)
        if _read_parquet(path) is not None:
            return
        _write_parquet(path, df)

    def snapshots_today(self, symbol: str) -> pd.DataFrame | None:
        return _read_parquet(self._path(symbol))

    def last_snapshot_of_day(self, symbol: str, day: date) -> pd.DataFrame | None:
        df = _read_parquet(self._path(symbol, day))
        if df is None or df.empty:
            return None
        return df[df["ts"] == df["ts"].iloc[-1]]

    def days_available(self, symbol: str) -> list[date]:
        folder = self.root / "chains" / symbol.upper()
        if not folder.exists():
            return []
        return sorted(date.fromisoformat(p.stem) for p in folder.glob("*.parquet"))


# ---------------------------------------------------------------------------
# Derived readers — every result carries its observation basis
# ---------------------------------------------------------------------------


def chain_delta_oi(chain, root: Path | None = None) -> dict | None:
    """Per-strike OI change vs the earliest stored snapshot that differs.

    Prefers previous session's last snapshot (classic prev-day ΔOI); falls
    back to today's first snapshot (intraday ΔOI). Returns None when there
    is no stored basis — the UI then shows '—' instead of a fabricated 0.
    """
    cs = ChainStore(root)
    sym = chain.symbol
    days = cs.days_available(sym)
    today = datetime.now(tz=IST).date()
    basis_df, basis_label = None, ""

    prev_days = [d for d in days if d < today]
    if prev_days:
        basis_df = cs.last_snapshot_of_day(sym, prev_days[-1])
        if basis_df is not None:
            basis_label = f"prev session close ({prev_days[-1].isoformat()})"
    if basis_df is None:
        today_df = cs.snapshots_today(sym)
        if today_df is not None and today_df["ts"].nunique() > 1:
            first_ts = today_df["ts"].iloc[0]
            basis_df = today_df[today_df["ts"] == first_ts]
            basis_label = f"intraday vs {pd.Timestamp(first_ts).tz_convert(IST).strftime('%H:%M')} IST"
    if basis_df is None:
        return None

    basis_df = basis_df[basis_df["expiry"] == str(chain.expiry)]
    if basis_df.empty:
        return None
    base = basis_df.set_index("strike")
    delta_call = np.full(len(chain.strikes), np.nan)
    delta_put = np.full(len(chain.strikes), np.nan)
    for i, k in enumerate(chain.strikes):
        if k in base.index:
            row = base.loc[k]
            delta_call[i] = chain.call_oi[i] - float(row["call_oi"])
            delta_put[i] = chain.put_oi[i] - float(row["put_oi"])
    return {
        "delta_call": delta_call,
        "delta_put": delta_put,
        "basis": basis_label,
        "basis_ts": str(basis_df["ts"].iloc[0]),
    }


def _atm_iv_of_snapshot(snap: pd.DataFrame) -> float | None:
    spot = float(snap["spot"].iloc[0])
    # A snapshot may carry spot=NaN — the capture stored "spot unavailable"
    # honestly instead of fabricating one. Without this guard the idxmin
    # below raises on the all-NA distance series and one such day 500s
    # every endpoint that walks the history.
    if np.isnan(spot):
        return None
    dist = (snap["strike"] - spot).abs()
    if dist.isna().all():
        return None
    idx = dist.idxmin()
    ivs = [v for v in (snap.loc[idx, "call_iv"], snap.loc[idx, "put_iv"])
           if v is not None and not np.isnan(v)]
    return float(np.mean(ivs)) if ivs else None


def atm_iv_history(symbol: str, root: Path | None = None) -> pd.Series:
    """One ATM-IV observation per captured day (last snapshot of each day)."""
    cs = ChainStore(root)
    out = {}
    for d in cs.days_available(symbol):
        snap = cs.last_snapshot_of_day(symbol, d)
        if snap is None or snap.empty:
            continue
        iv = _atm_iv_of_snapshot(snap)
        if iv is not None:
            out[d] = iv
    return pd.Series(out, dtype=float).sort_index()


def iv_rank_local(symbol: str, current_iv: float, root: Path | None = None) -> dict:
    """True IV rank from locally captured history — or an honest refusal.

    rank = percentile of current ATM IV within the captured daily series.
    Below MIN_IV_RANK_DAYS observations we return captured/required counts
    and NO rank value: a rank computed on 3 days would be a fake number.
    """
    hist = atm_iv_history(symbol, root)
    n = len(hist)
    if n < MIN_IV_RANK_DAYS:
        return {
            "available": False,
            "days_captured": n,
            "days_required": MIN_IV_RANK_DAYS,
            "note": f"capturing — {n}d of {MIN_IV_RANK_DAYS}d minimum local history",
        }
    rank = float((hist <= current_iv).mean())
    return {
        "available": True,
        "rank": rank,
        "days_captured": n,
        "min_iv": float(hist.min()),
        "max_iv": float(hist.max()),
        "first_day": hist.index[0].isoformat(),
        "last_day": hist.index[-1].isoformat(),
    }


def straddle_series(symbol: str, expiry: str | None = None,
                    root: Path | None = None) -> list[dict]:
    """ATM straddle premium across today's snapshots (real captures only).

    `expiry` (YYYY-MM-DD) restricts the series to one expiry — two expiries
    interleaved would draw a saw-tooth that never traded.
    """
    cs = ChainStore(root)
    df = cs.snapshots_today(symbol)
    if df is None or df.empty:
        return []
    if expiry is not None:
        df = df[df["expiry"] == expiry]
        if df.empty:
            return []
    out = []
    for ts, snap in df.groupby("ts", sort=True):
        spot = float(snap["spot"].iloc[0])
        idx = (snap["strike"] - spot).abs().idxmin()
        straddle = float(snap.loc[idx, "call_ltp"] + snap.loc[idx, "put_ltp"])
        out.append({
            "time": int(pd.Timestamp(ts).timestamp()),
            "value": straddle,
            "strike": float(snap.loc[idx, "strike"]),
            "spot": spot,
        })
    return out


# ---------------------------------------------------------------------------
# Coverage stats — what the store actually holds (for the DTA view)
# ---------------------------------------------------------------------------


def store_stats(root: Path | None = None) -> dict:
    root = root or STORE_DIR
    def _tree_size(p: Path) -> int:
        return sum(f.stat().st_size for f in p.rglob("*.parquet")) if p.exists() else 0

    bars_dir, chains_dir, ticks_dir = root / "bars1m", root / "chains", root / "ticks"
    bars = {}
    if bars_dir.exists():
        for sym_dir in sorted(bars_dir.iterdir()):
            if sym_dir.is_dir():
                days = sorted(p.stem for p in sym_dir.glob("*.parquet"))
                if days:
                    bars[sym_dir.name] = {"days": len(days), "first": days[0], "last": days[-1]}
    chains = {}
    if chains_dir.exists():
        for sym_dir in sorted(chains_dir.iterdir()):
            if sym_dir.is_dir():
                days = sorted(p.stem for p in sym_dir.glob("*.parquet"))
                snaps = 0
                last = _read_parquet(sym_dir / f"{days[-1]}.parquet") if days else None
                if last is not None:
                    snaps = int(last["ts"].nunique())
                if days:
                    chains[sym_dir.name] = {
                        "days": len(days), "first": days[0], "last": days[-1],
                        "snapshots_today": snaps,
                    }
    return {
        "root": str(root),
        "size_bytes": _tree_size(root),
        "bars": bars,
        "chains": chains,
        "tick_days": len(list(ticks_dir.iterdir())) if ticks_dir.exists() else 0,
    }


# ---------------------------------------------------------------------------
# History archive: daily candles that accumulate in the background
# ---------------------------------------------------------------------------


class HistoryArchive:
    """Daily-candle archive under store/history/SYMBOL.parquet.

    The server's sync loop upserts fresh candles for the pulse boards and
    watchlist on a cadence, so the local archive keeps growing while the
    terminal runs — bulk exports can then come from disk even when a source
    throttles. Real data only: the loop never runs in offline mode, and
    upsert() refuses rows from a synthetic source.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or STORE_DIR) / "history"

    def _path(self, symbol: str) -> Path:
        safe = symbol.replace("/", "_").replace("^", "_").replace("=", "_")
        return self.root / f"{safe}.parquet"

    def upsert(self, symbol: str, df: pd.DataFrame, source: str) -> int:
        """Merge daily candles by date (newest wins). Returns rows on disk."""
        if "synthetic" in source.lower() or "demo" in source.lower():
            raise ValueError("synthetic data is never written to the store")
        cols = {c.lower(): c for c in df.columns}
        keep = {k: cols[k] for k in ("open", "high", "low", "close", "volume")
                if k in cols}
        idx = pd.to_datetime(df.index)
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        fresh = pd.DataFrame({k: df[v].to_numpy() for k, v in keep.items()})
        fresh.insert(0, "date", idx.normalize())
        fresh["source"] = source
        fresh["captured_at"] = _now_utc().isoformat(timespec="seconds")

        prior = _read_parquet(self._path(symbol))
        if prior is not None:
            merged = pd.concat([prior, fresh], ignore_index=True)
            merged["date"] = pd.to_datetime(merged["date"])
            merged = merged.drop_duplicates(subset="date", keep="last")
        else:
            merged = fresh
        merged = merged.sort_values("date").reset_index(drop=True)
        _write_parquet(self._path(symbol), merged)
        return len(merged)

    def read(self, symbol: str) -> pd.DataFrame | None:
        return _read_parquet(self._path(symbol))

    def stats(self) -> dict:
        out, total = {}, 0
        if self.root.exists():
            for p in sorted(self.root.glob("*.parquet")):
                df = _read_parquet(p)
                if df is None or df.empty:
                    continue
                total += p.stat().st_size
                out[p.stem] = {
                    "rows": len(df),
                    "first": str(pd.to_datetime(df["date"]).min().date()),
                    "last": str(pd.to_datetime(df["date"]).max().date()),
                    "source": str(df["source"].iloc[-1]),
                }
        return {"symbols": out, "size_bytes": total, "root": str(self.root)}
