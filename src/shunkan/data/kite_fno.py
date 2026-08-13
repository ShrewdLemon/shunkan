"""Kite Connect F&O data: instruments dump, option chains, historical candles.

This is the proper live derivatives source once Zerodha is connected —
exchange-grade OI and prices straight from the broker, no NSE scraping.

The instruments dump (~100k rows of every tradable contract) is fetched
once per day and cached as parquet; chain construction then needs exactly
one batched /quote call (≤500 instruments per request).
"""

from __future__ import annotations

import io
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from shunkan.config import CACHE_DIR, ensure_dirs
from shunkan.data.brokers import KiteProvider
from shunkan.data.memcache import ttl_cache
from shunkan.data.provider import DataError, is_offline
from shunkan.derivatives.chain import OptionChain
from shunkan.derivatives.synthetic import STRIKE_STEPS
from shunkan.markets import (
    IST,
    MARKET_CLOSE,
    is_expired,
    time_to_expiry_years,
    today_ist,
)

# Kite quotes indices under their full NSE names.
INDEX_KITE_NAMES = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
}

_INSTRUMENTS_TTL = 20 * 3600  # the dump updates once daily (~8 AM IST)


def load_instruments(
    kite: KiteProvider | None = None, exchange: str = "NFO"
) -> pd.DataFrame:
    """Daily instruments dump for an exchange, disk-cached.

    kite=None fetches the same dump unauthenticated — Kite serves
    /instruments without credentials, so the exchange's own contract lots
    are available to every user, broker connected or not.
    """
    ensure_dirs()
    path = CACHE_DIR / f"kite_instruments_{exchange}.parquet"
    if path.exists() and time.time() - path.stat().st_mtime < _INSTRUMENTS_TTL:
        try:
            return pd.read_parquet(path)
        except Exception:
            pass
    if kite is not None:
        text = kite.get_text(f"/instruments/{exchange}")
    else:
        import httpx

        try:
            # Connect fails fast when the network is down; the body itself is
            # ~3 MB, so the read budget is deliberately longer.
            resp = httpx.get(f"{KiteProvider.BASE}/instruments/{exchange}",
                             timeout=httpx.Timeout(20.0, connect=5.0))
            resp.raise_for_status()
            text = resp.text
        except Exception as exc:
            raise DataError(f"Instruments dump unavailable: {exc}") from exc
    df = parse_instruments_csv(text)
    try:
        df.to_parquet(path)
    except Exception:
        pass
    return df


def _one_lot_size(opts: pd.DataFrame) -> int | None:
    """The lot size every row agrees on, or None.

    Disagreement means a lot revision straddles these rows; picking a side
    would be a coin flip on a number that multiplies every rupee figure.
    """
    lots = pd.to_numeric(opts["lot_size"], errors="coerce").dropna()
    unique = np.unique(lots[lots > 0].astype(int).to_numpy())
    return int(unique[0]) if len(unique) == 1 else None


@ttl_cache(ttl=900.0, max_items=64)
def cached_lot_size(symbol: str, expiry: date | None = None) -> tuple[int | None, str]:
    """Contract lot for a symbol, from the exchange's own instruments dump.

    Needs no broker and at most one download a day (the dump is disk-cached
    by load_instruments). Returns (None, why) when the dump cannot be had:
    a guessed lot silently multiplies every rupee figure downstream, so an
    honest dash beats a plausible number.
    """
    if is_offline():
        return None, "no lot size — offline mode (SHUNKAN_OFFLINE=1)"
    sym = symbol.upper().removesuffix(".NS")
    try:
        df = load_instruments(exchange="NFO")
        opts = df[(df["name"] == sym) & (df["instrument_type"].isin(["CE", "PE"]))]
        if opts.empty:
            return None, f"no lot size — {sym} has no NFO contracts in the dump"
        if expiry is not None and (opts["expiry"] == expiry).any():
            opts = opts[opts["expiry"] == expiry]  # the exact series being priced
        else:
            live = {e for e in opts["expiry"].dropna().unique() if not is_expired(e)}
            opts = opts[opts["expiry"].isin(live)]
        lot = _one_lot_size(opts)
    except Exception as exc:  # a lot lookup must never break the chain
        return None, f"no lot size — instruments dump unavailable ({str(exc)[:80]})"
    if lot is None:
        return None, f"no lot size — no single lot for {sym} in the NFO dump"
    path = CACHE_DIR / "kite_instruments_NFO.parquet"
    day = date.fromtimestamp(path.stat().st_mtime) if path.exists() else today_ist()
    return lot, f"NFO instruments dump ({day})"


def parse_instruments_csv(text: str) -> pd.DataFrame:
    """Parse Kite's instruments CSV (pure function — unit-testable offline)."""
    df = pd.read_csv(io.StringIO(text))
    required = {"instrument_token", "tradingsymbol", "name", "expiry", "strike",
                "lot_size", "instrument_type", "segment", "exchange"}
    missing = required - set(df.columns)
    if missing:
        raise DataError(f"Instruments dump missing columns: {missing}")
    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce").dt.date
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce").fillna(0.0)
    return df


def kite_spot(kite: KiteProvider, symbol: str) -> float:
    sym = symbol.upper().removesuffix(".NS")
    instrument = INDEX_KITE_NAMES.get(sym, f"NSE:{sym}")
    data = kite.get_json("/quote", params=[("i", instrument)]).get("data", {})
    payload = data.get(instrument) or {}
    spot = float(payload.get("last_price") or 0.0)
    if spot <= 0:
        raise DataError(f"Kite returned no spot for {instrument}")
    return spot


def kite_option_chain(
    kite: KiteProvider,
    symbol: str,
    expiry: date | None = None,
    width: int = 12,
) -> OptionChain:
    """Build an OptionChain from Kite: instruments dump + one batch quote.

    width: number of strikes on each side of ATM to include.
    """
    sym = symbol.upper().removesuffix(".NS")
    spot = kite_spot(kite, sym)
    instruments = load_instruments(kite, "NFO")

    opts = instruments[
        (instruments["name"] == sym)
        & (instruments["instrument_type"].isin(["CE", "PE"]))
    ]
    if opts.empty:
        raise DataError(f"No NFO option contracts found for {sym}")

    expiries = sorted(
        e for e in opts["expiry"].dropna().unique() if not is_expired(e)
    )
    if not expiries:
        raise DataError(f"No live expiries for {sym}")
    chosen = expiry if expiry in expiries else expiries[0]
    opts = opts[opts["expiry"] == chosen]

    strikes_all = np.sort(opts["strike"].unique())
    step = STRIKE_STEPS.get(sym, float(np.median(np.diff(strikes_all))) or 1.0)
    atm = strikes_all[np.argmin(np.abs(strikes_all - spot))]
    window = strikes_all[
        (strikes_all >= atm - width * step) & (strikes_all <= atm + width * step)
    ]
    opts = opts[opts["strike"].isin(window)]

    # One batched quote for every CE/PE in the window (Kite caps at 500/call).
    keys = [f"NFO:{ts}" for ts in opts["tradingsymbol"]]
    quotes: dict[str, dict] = {}
    for i in range(0, len(keys), 450):
        batch = keys[i : i + 450]
        data = kite.get_json("/quote", params=[("i", k) for k in batch]).get("data", {})
        quotes.update(data)

    strikes = np.sort(opts["strike"].unique())
    n = len(strikes)
    cols = {k: np.zeros(n) for k in
            ("c_ltp", "c_oi", "c_vol", "p_ltp", "p_oi", "p_vol")}
    # Authoritative per contract; a disagreeing or unusable column yields
    # None rather than int(NaN) or a coin-flip multiplier.
    lot_size = _one_lot_size(opts)

    idx = {s: i for i, s in enumerate(strikes)}
    for _, row in opts.iterrows():
        q = quotes.get(f"NFO:{row['tradingsymbol']}")
        if not q:
            continue
        i = idx[row["strike"]]
        side = "c" if row["instrument_type"] == "CE" else "p"
        cols[f"{side}_ltp"][i] = float(q.get("last_price") or 0.0)
        cols[f"{side}_oi"][i] = float(q.get("oi") or 0.0)
        cols[f"{side}_vol"][i] = float(q.get("volume") or 0.0)

    t_years = time_to_expiry_years(chosen)
    nan = np.full(n, np.nan)
    return OptionChain(
        symbol=sym,
        spot=spot,
        expiry=chosen,
        t_years=t_years,
        strikes=strikes.astype(np.float64),
        call_ltp=cols["c_ltp"],
        call_oi=cols["c_oi"],
        call_oi_change=np.zeros(n),  # Kite /quote has no prev-day OI delta
        call_volume=cols["c_vol"],
        call_iv=nan.copy(),  # solved from prices on demand
        put_ltp=cols["p_ltp"],
        put_oi=cols["p_oi"],
        put_oi_change=np.zeros(n),
        put_volume=cols["p_vol"],
        put_iv=nan.copy(),
        source="Zerodha Kite (real-time)",
        is_model=False,
        lot_size=lot_size,
        lot_size_source=(f"NFO instruments dump ({chosen} series)"
                         if lot_size else "no single lot in the instruments dump"),
        expiries=list(expiries),
    )


_KITE_INTERVALS = {
    "1m": "minute", "5m": "5minute", "15m": "15minute",
    "30m": "30minute", "1h": "60minute", "1d": "day",
}

# Kite rejects ranges longer than these per interval (HTTP 400). Defaults
# and explicit requests are clamped to the cap, measured back from `end`.
_KITE_MAX_DAYS = {
    "1m": 55, "5m": 90, "15m": 90, "30m": 90, "1h": 350, "1d": 1900,
}


def kite_historical(
    kite: KiteProvider,
    symbol: str,
    interval: str = "1d",
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Historical OHLCV candles from Kite for an NSE cash/index symbol."""
    if interval not in _KITE_INTERVALS:
        raise DataError(f"Kite interval must be one of {list(_KITE_INTERVALS)}")
    sym = symbol.upper().removesuffix(".NS")
    nse = load_instruments(kite, "NSE")
    match = nse[nse["tradingsymbol"] == sym]
    if match.empty:
        raise DataError(f"{sym} not found in Kite NSE instruments")
    token = int(match["instrument_token"].iloc[0])

    end = end or datetime.now()
    max_days = _KITE_MAX_DAYS[interval]
    start = start or end - pd.Timedelta(days=max_days)
    if (end - start).days > max_days:
        start = end - pd.Timedelta(days=max_days)
    payload = kite.get_json(
        f"/instruments/historical/{token}/{_KITE_INTERVALS[interval]}",
        params={"from": start.strftime("%Y-%m-%d %H:%M:%S"),
                "to": end.strftime("%Y-%m-%d %H:%M:%S")},
    )
    candles = payload.get("data", {}).get("candles", [])
    if not candles:
        raise DataError(f"Kite returned no candles for {sym}")
    df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"])
    return df.set_index("time")


# ---------------------------------------------------------------------------
# ΔOI bootstrap — the buildup column on day one
# ---------------------------------------------------------------------------
#
# Kite's /quote carries live `oi` but no previous-day close, so kite_option_chain
# leaves call_oi_change/put_oi_change at zero and the UI honestly shows "—".
# The store's ΔOI reader only needs one prior snapshot to work, and the
# exchange's own settled day-candles carry OI when asked with oi=1 — so we can
# seed exactly that snapshot instead of waiting a session to capture one.
#
# Deliberately NOT used: /quote's `oi_day_low`. It equals the previous close
# only while OI rises all day; on an unwinding day it is simply the day's
# trough. A number that is right most of the time and silently wrong on the
# days that matter is worse than no number.

_HIST_RATE_DELAY = 0.34  # Kite historical is 3 req/sec
_backfilled: set[tuple[str, str]] = set()


def _day_oi(kite: KiteProvider, token: int, before: date) -> tuple[date, float] | None:
    """Last settled (date, open_interest) strictly before `before`."""
    j = kite.get_json(
        f"/instruments/historical/{token}/day",
        {"from": str(before - timedelta(days=10)), "to": str(before), "oi": 1},
    )
    for candle in reversed(j.get("data", {}).get("candles", []) or []):
        # candle: [ts, o, h, l, c, volume, oi]
        day = datetime.fromisoformat(candle[0]).date()
        if day < before and len(candle) >= 7:
            return day, float(candle[6])
    return None


def backfill_prev_close_oi(kite: KiteProvider, chain: OptionChain,
                           root=None) -> tuple[bool, str]:
    """Seed the store with the previous session's closing OI for this chain.

    Costs two historical calls per strike at 3 req/sec, so it runs once per
    (symbol, expiry) per process. Returns (wrote_anything, why).
    """
    from shunkan.store import ChainStore

    key = (chain.symbol.upper(), str(chain.expiry))
    if key in _backfilled:
        return False, "already attempted this session"
    _backfilled.add(key)

    store = ChainStore(root)
    today = today_ist()
    # The basis has to match the SERIES, not just the symbol: a stored day
    # full of last month's expiry is no basis for this week's chain, and
    # chain_delta_oi filters on expiry before it ever looks at the numbers.
    for day in store.days_available(chain.symbol):
        if day >= today:
            continue
        prior = store.last_snapshot_of_day(chain.symbol, day)
        if prior is not None and (prior["expiry"] == str(chain.expiry)).any():
            return False, f"real prior snapshot for this expiry exists ({day})"

    try:
        nfo = load_instruments(kite, "NFO")
    except DataError as exc:
        return False, f"instruments dump unavailable ({exc})"
    opts = nfo[(nfo["name"] == chain.symbol.upper())
               & (nfo["expiry"] == chain.expiry)
               & (nfo["instrument_type"].isin(["CE", "PE"]))]
    if opts.empty:
        return False, f"no NFO contracts for {chain.symbol} {chain.expiry}"
    tokens = {(float(r.strike), r.instrument_type): int(r.instrument_token)
              for r in opts.itertuples()}

    basis_day, call_oi, put_oi = None, [], []
    for k in chain.strikes:
        for side, sink in (("CE", call_oi), ("PE", put_oi)):
            tok = tokens.get((float(k), side))
            got = None
            if tok is not None:
                try:
                    got = _day_oi(kite, tok, today)
                except Exception:
                    got = None  # one dead contract must not void the basis
                time.sleep(_HIST_RATE_DELAY)
            if got is None:
                sink.append(np.nan)
            else:
                basis_day = max(basis_day or got[0], got[0])
                sink.append(got[1])

    if basis_day is None or not np.any(np.isfinite(call_oi)):
        return False, "no settled OI returned for any strike"

    n = len(chain.strikes)
    nan = np.full(n, np.nan)
    ts = datetime.combine(basis_day, MARKET_CLOSE, tzinfo=IST).isoformat(timespec="seconds")
    store.write_day_snapshot(chain.symbol, basis_day, pd.DataFrame({
        "ts": [ts] * n,
        "expiry": [str(chain.expiry)] * n,
        "spot": [float("nan")] * n,   # not observed — never invent one
        "strike": chain.strikes,
        "call_ltp": nan.copy(), "call_oi": np.asarray(call_oi, dtype=float),
        "call_iv": nan.copy(), "call_volume": nan.copy(),
        "put_ltp": nan.copy(), "put_oi": np.asarray(put_oi, dtype=float),
        "put_iv": nan.copy(), "put_volume": nan.copy(),
        "source": [f"Kite historical day-candle OI ({basis_day})"] * n,
    }))
    return True, f"seeded ΔOI basis from {basis_day} close"


# ---------------------------------------------------------------------------
# Instruments archive — the only path to a self-owned options history
# ---------------------------------------------------------------------------
#
# Exchanges flush F&O instrument_tokens at every expiry, and Kite's docs are
# explicit that expired contracts' tokens cannot be retrieved afterwards
# "unless you regularly download and cache them". So a day not archived is a
# day of options history that can never be reconstructed, from any source, at
# any price. The dump is ~3 MB and needs no credentials — archiving is close
# to free and strictly one-way in value.


def archive_instruments_dump(exchange: str = "NFO", root=None,
                             kite: KiteProvider | None = None) -> tuple[bool, str]:
    """Keep today's contract master. Idempotent; safe to call on every boot."""
    from shunkan.store.store import STORE_DIR

    root = root or STORE_DIR
    day = today_ist()
    folder = root / "instruments" / exchange.upper()
    path = folder / f"{day.isoformat()}.parquet"
    if path.exists():
        return False, f"already archived {day}"
    try:
        df = load_instruments(kite, exchange)
    except DataError as exc:
        return False, f"dump unavailable ({exc})"
    folder.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    return True, f"archived {len(df):,} {exchange} contracts for {day}"


def archived_instrument_days(exchange: str = "NFO", root=None) -> list[date]:
    from shunkan.store.store import STORE_DIR

    folder = (root or STORE_DIR) / "instruments" / exchange.upper()
    if not folder.exists():
        return []
    return sorted(date.fromisoformat(p.stem) for p in folder.glob("*.parquet"))


# ---------------------------------------------------------------------------
# Basket margin — the only honest way to size a multi-leg F&O position
# ---------------------------------------------------------------------------
#
# SPAN+exposure for a spread is far less than for its legs priced separately,
# because the exchange nets offsetting risk. Any local approximation would
# either overstate a hedged book badly or understate a naked one dangerously,
# so Shunkan asks the exchange's own calculator or reports nothing.


def tradingsymbol_for(instrument, dump: pd.DataFrame | None = None) -> str | None:
    """Kite's contract name for an Instrument, or None if it is not listed.

    Looks in the instrument's OWN venue: a SENSEX option lives in BFO and a
    CRUDEOIL future in MCX, neither of which appear in the NFO dump.
    """
    if not instrument.derivative:
        return instrument.symbol
    df = dump if dump is not None else load_instruments(exchange=instrument.exchange)
    kind = "FUT" if instrument.kind == "FUT" else instrument.kind
    match = df[(df["name"] == instrument.symbol)
               & (df["expiry"] == instrument.expiry)
               & (df["instrument_type"] == kind)]
    if instrument.strike is not None:
        match = match[match["strike"] == instrument.strike]
    return None if match.empty else str(match["tradingsymbol"].iloc[0])


def basket_margin(kite: KiteProvider, legs: list[dict]) -> dict:
    """Exchange-priced margin for a whole basket.

    `legs` are {instrument, side, quantity}. Returns the initial (each leg
    standalone) and final (netted across the basket) breakdown — the gap
    between them is the hedge benefit, which is exactly what makes a spread
    affordable and a naked short not.
    """
    # One dump per venue in the basket — a desk can hold NFO index options and
    # an MCX crude hedge in the same book, and each lives in its own file.
    dumps: dict[str, pd.DataFrame] = {}
    orders, unpriceable = [], []
    for leg in legs:
        inst = leg["instrument"]
        try:
            if inst.exchange not in dumps:
                dumps[inst.exchange] = load_instruments(exchange=inst.exchange)
            ts = tradingsymbol_for(inst, dumps[inst.exchange])
        except DataError:
            ts = None
        if ts is None:
            unpriceable.append(inst.label)
            continue
        orders.append({
            "exchange": inst.exchange,
            "tradingsymbol": ts,
            "transaction_type": leg["side"].upper(),
            "variety": "regular",
            "product": "NRML" if inst.derivative else "CNC",
            "order_type": "MARKET",
            "quantity": int(abs(leg["quantity"])),
        })
    if not orders:
        raise DataError(f"nothing priceable in the basket ({', '.join(unpriceable) or 'empty'})")

    payload = kite.post_json("/margins/basket?consider_positions=true", orders)
    data = payload.get("data") or {}

    def block(d: dict) -> dict:
        return {
            "span": float(d.get("span") or 0.0),
            "exposure": float(d.get("exposure") or 0.0),
            "option_premium": float(d.get("option_premium") or 0.0),
            "total": float(d.get("total") or 0.0),
        }

    initial, final = block(data.get("initial") or {}), block(data.get("final") or {})
    return {
        "initial": initial,
        "final": final,
        "hedge_benefit": max(initial["total"] - final["total"], 0.0),
        "unpriceable": unpriceable,   # named, never silently dropped from the total
        "source": "Zerodha SPAN calculator (/margins/basket)",
    }
