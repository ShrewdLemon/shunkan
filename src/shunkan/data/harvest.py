"""Harvest the life of every listed option contract before it expires.

The mistake this module exists to correct: option history was assumed to be
unobtainable, so the plan was to capture chains forward and wait months for a
dataset. That is only half true, and the wrong half.

Kite deletes an expired contract from the instruments master entirely. Measured
2026-08-17: of 2,682 live NIFTY and BANKNIFTY option rows, exactly 0 have an
expiry before today. Once a contract expires you cannot fetch its candles and
cannot even name its token, so nothing recovers it.

But a contract that is STILL LISTED carries its whole life. NIFTY2681824400CE,
expiring 2026-08-18, returns 24 day candles with open interest going back to
2026-07-15 right now. A December expiry listed last July returns hundreds. So
there is a real back-history sitting in the API today, and it evaporates one
expiry at a time.

That makes this the only genuinely time-critical job in the codebase. Everything
else can be built next week at the same cost. A weekly expiry not harvested
before its Tuesday is a permanent hole in the surface history.

Rate: Kite's historical endpoint tolerates ~3 requests/second, so a full sweep
of both indices is minutes, not hours. Cheap insurance against an irreversible
loss.

WHAT THIS ARCHIVE IS NOT, and it took a research pass to notice
A contract appears here for a past date only if it was STILL LISTED when the
harvest ran. That is selection on a future variable, and it makes the long tail
of this archive useless for research even though it looks impressive.

Measured on the first sweep: 133,968 rows across 271 sessions back to
2025-07-14, of which 69.8% had zero volume, 56.5% had zero open interest, and
only 26 of 271 sessions contained any contract within 45 days of expiry. The
median contract in January 2026 had 711 days to expiry. So the "13 months of
option history" is roughly two months of genuine near-chain data bolted onto
eleven months of illiquid LEAPS that happened to survive to harvest day.

Worse: ZERO rows have date >= expiry, so the archive has never observed a
single settlement. Every expiry-day question is unanswerable, not merely
noisy.

Both problems have the same fix and it is forward-looking: harvest each expiry
ON its expiry day after the close, before Kite drops the contract overnight.
Do that and the archive accumulates unbiased near-chain data with settlements.
Nothing recovers the back-history; it was never really there.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from shunkan.data.provider import DataError
from shunkan.markets import today_ist

# Kite historical is ~3 req/s. kite_fno uses the same figure for its dOI basis.
_RATE_DELAY = 0.34

# Day candles reach back to listing; anything longer is wasted range on a
# contract that cannot have existed yet.
_MAX_LOOKBACK_DAYS = 400


@dataclass
class HarvestResult:
    symbol: str
    interval: str
    contracts_seen: int = 0
    contracts_written: int = 0
    candles_written: int = 0
    empty: int = 0            # listed but never traded, which is most far wings
    failed: int = 0
    expiries: dict = field(default_factory=dict)
    elapsed_s: float = 0.0
    errors: list = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.symbol} {self.interval}: {self.candles_written:,} candles "
                f"from {self.contracts_written:,}/{self.contracts_seen:,} contracts "
                f"({self.empty:,} never traded, {self.failed} failed) "
                f"in {self.elapsed_s:.0f}s")


def contracts_dir(root=None):
    from shunkan.store.store import STORE_DIR

    d = (root or STORE_DIR) / "contracts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _candles(kite, token: int, start: date, end: date, interval: str) -> list:
    """oi=1 is the whole point: an option candle without open interest cannot
    answer any positioning question later."""
    j = kite.get_json(
        f"/instruments/historical/{token}/{interval}",
        {"from": str(start), "to": str(end), "oi": 1},
    )
    return j.get("data", {}).get("candles", []) or []


def harvest_contract_lives(
    kite,
    symbols: tuple[str, ...] = ("NIFTY", "BANKNIFTY"),
    interval: str = "day",
    expiry: date | None = None,
    root=None,
    max_contracts: int | None = None,
    progress=None,
) -> list[HarvestResult]:
    """Pull and store the full traded life of every listed option contract.

    `expiry` restricts to one series, which is what you want when an expiry is
    hours from vanishing. Without it, everything listed is swept.

    Writes one parquet per (symbol, expiry) under store/contracts, upserted on
    (tradingsymbol, ts) so re-running is safe and idempotent: the point is to
    run this every day, and a job you are afraid to re-run does not get run.
    """
    from shunkan.data.kite_fno import load_instruments

    out: list[HarvestResult] = []
    nfo = load_instruments(kite, "NFO")
    today = today_ist()
    start = today - timedelta(days=_MAX_LOOKBACK_DAYS)

    for sym in symbols:
        res = HarvestResult(symbol=sym, interval=interval)
        t0 = time.perf_counter()
        opts = nfo[(nfo["name"] == sym)
                   & (nfo["instrument_type"].isin(["CE", "PE"]))]
        if expiry is not None:
            opts = opts[opts["expiry"] == expiry]
        # Nearest expiry first: it is the one that disappears soonest, so if the
        # run is interrupted the irreplaceable part is already on disk.
        opts = opts.sort_values(["expiry", "strike"])
        if max_contracts:
            opts = opts.head(max_contracts)
        res.contracts_seen = len(opts)

        by_expiry: dict[date, list] = {}
        for i, row in enumerate(opts.itertuples(), 1):
            try:
                candles = _candles(kite, int(row.instrument_token), start, today, interval)
            except Exception as exc:
                res.failed += 1
                if len(res.errors) < 5:
                    res.errors.append(f"{row.tradingsymbol}: {str(exc)[:100]}")
                time.sleep(_RATE_DELAY)
                continue
            time.sleep(_RATE_DELAY)

            if not candles:
                # Listed but never traded. Common for far wings and not an
                # error: recording it as one would bury the real failures.
                res.empty += 1
                continue
            for c in candles:
                if len(c) < 7:
                    continue
                by_expiry.setdefault(row.expiry, []).append({
                    "ts": c[0], "tradingsymbol": row.tradingsymbol,
                    "expiry": str(row.expiry), "strike": float(row.strike),
                    "right": row.instrument_type, "lot_size": int(row.lot_size),
                    "open": c[1], "high": c[2], "low": c[3], "close": c[4],
                    "volume": c[5], "oi": c[6],
                })
            res.contracts_written += 1
            if progress and i % 25 == 0:
                progress(f"  {sym} {i}/{res.contracts_seen} "
                         f"({res.contracts_written} traded, {res.empty} empty)")

        for exp, rows in by_expiry.items():
            n = _upsert(contracts_dir(root) / f"{sym}_{exp}_{interval}.parquet", rows)
            res.expiries[str(exp)] = n
            res.candles_written += len(rows)
        res.elapsed_s = time.perf_counter() - t0
        out.append(res)
    return out


def _upsert(path, rows: list[dict]) -> int:
    """Merge on (tradingsymbol, ts), newest wins. Returns rows on disk."""
    fresh = pd.DataFrame(rows)
    if path.exists():
        try:
            fresh = pd.concat([pd.read_parquet(path), fresh])
        except Exception:
            # Quarantine, never overwrite. An "unreadable" file here is as
            # likely to be another process mid-write as real corruption, and
            # the news archive lost 10,084 rows to a fallback that assumed
            # otherwise. The bytes are kept for forensics either way.
            import time as _t

            path.rename(path.with_suffix(f".{int(_t.time())}.corrupt.parquet"))
    fresh = (fresh.drop_duplicates(subset=["tradingsymbol", "ts"], keep="last")
                  .sort_values(["ts", "strike", "right"]))
    fresh.to_parquet(path, index=False)
    return len(fresh)


def settling_today(kite, symbols=("NIFTY", "BANKNIFTY")) -> list:
    """Expiries settling today. These are the harvest that matters most.

    A contract is dropped from the instruments master overnight, so its final
    session, the one containing settlement, is only fetchable between the close
    and the drop. Miss that window and the expiry is in the archive with its
    last day missing, which is precisely the day every expiry question needs.
    """
    from shunkan.data.kite_fno import load_instruments

    nfo = load_instruments(kite, "NFO")
    today = today_ist()
    opts = nfo[(nfo["name"].isin(symbols))
               & (nfo["instrument_type"].isin(["CE", "PE"]))
               & (nfo["expiry"] == today)]
    return sorted(opts["expiry"].unique())


def expiring_soon(kite, symbols=("NIFTY", "BANKNIFTY"), within_days: int = 2):
    """Contracts about to become unrecoverable. Drives the pre-open job."""
    from shunkan.data.kite_fno import load_instruments

    nfo = load_instruments(kite, "NFO")
    today = today_ist()
    cutoff = today + timedelta(days=within_days)
    opts = nfo[(nfo["name"].isin(symbols))
               & (nfo["instrument_type"].isin(["CE", "PE"]))
               & (nfo["expiry"] >= today) & (nfo["expiry"] <= cutoff)]
    return sorted(opts["expiry"].unique())


def coverage(root=None) -> pd.DataFrame:
    """What the archive holds, so a gap is visible rather than assumed absent."""
    rows = []
    for f in sorted(contracts_dir(root).glob("*.parquet")):
        if f.name.endswith(".corrupt.parquet"):
            continue
        try:
            df = pd.read_parquet(f, columns=["ts", "tradingsymbol", "oi"])
        except Exception:
            continue
        sym, exp, interval = f.stem.split("_", 2)
        ts = pd.to_datetime(df["ts"], format="mixed", utc=True)
        rows.append({
            "symbol": sym, "expiry": exp, "interval": interval,
            "contracts": df["tradingsymbol"].nunique(), "candles": len(df),
            "first": ts.min().date(), "last": ts.max().date(),
            "with_oi": int((df["oi"] > 0).sum()),
        })
    return pd.DataFrame(rows)
