"""The public facts card: end-of-day positioning, flows, expiry and the 48-hour calendar.

compose() reads the store only: it never raises and never touches NSE, and
every section is data with as_of and source, or {"error", "source"}. Provider
bars that fill a gap in the history archive stay in memory. refresh() is the
only fetcher. Bhavcopy OI is in units; contracts = units / lot, and lot is
published only from 2024-07-08.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from shunkan.analytics.daily import _max_pain
from shunkan.data import events_calendar as cal
from shunkan.data import flows, fo_bhav, fo_iv, fo_store, index_bars, participant
from shunkan.data.provider import DataError, Unpublished, is_offline
from shunkan.markets import INDEX_ALIASES, IST

OPEN, CLOSE = time(9, 15), time(15, 30)
LOOKBACK = 5
SECTIONS = ("session", "price", "positioning", "flows", "expiry", "calendar")
POST_CLOSE_WARNING = "today's bhavcopy not on disk yet; the {} session has closed"
EXPIRY_WARNING = ("expiry day: OI walls expire at 15:30 and overnight OI is mostly closing "
                  "positions; read intraday migration, not these levels.")
SESSION_SRC = "NSE F&O bhavcopies on disk; NSE holiday-master (F&O) cache"
PRICE_SRC = "history archive"
FLOWS_SRC = "NSE participant OI, fii_stats and fiidiiTradeReact files on disk"
CAL_SRC = "NSE holiday-master, board-meeting and bhavcopy caches; packaged RBI, FOMC and CPI dates"
PART_SRC = "NSE fao_participant_oi"
PART_STORE_SRC = "NSE participant store (fao_oi.parquet)"
FII_SRC = "NSE fii_stats"
CASH_SRC = f"NSE fiidiiTradeReact ({flows.BASIS})"


def _ist(dt: datetime) -> datetime:
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def _now(now) -> datetime:
    return datetime.now(IST) if now is None else _ist(now)


def _day(d) -> date:
    return pd.Timestamp(d).date()


def _msg(exc: Exception) -> str:
    return str(exc) if isinstance(exc, DataError) else f"{type(exc).__name__}: {exc}"


def _guard(source: str, fn) -> dict:
    try:
        return fn()
    except Exception as exc:
        return {"error": _msg(exc), "source": source}


def _store(root) -> Path:
    if root is None:
        from shunkan.store.store import STORE_DIR

        return STORE_DIR
    return Path(root)


def _bhav_src(as_of) -> str:
    return f"NSE F&O bhavcopy {as_of} (end of day)" if as_of else "NSE F&O bhavcopy"


def _plain(x):
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if x is pd.NaT:
        return None
    if isinstance(x, np.generic):
        x = x.item()
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    return x


def _captured(v) -> datetime | None:
    try:
        ts = pd.to_datetime(v, utc=True)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    return ts.tz_convert(IST).to_pydatetime()


def settled_bars(hist: pd.DataFrame, now: datetime) -> tuple[pd.DataFrame, dict | None]:
    now = _ist(now)
    if hist is None or len(hist) == 0:
        return hist, None
    dates = pd.to_datetime(hist["date"]).dt.normalize()
    today = pd.Timestamp(now.date())
    close_at = datetime.combine(now.date(), CLOSE, IST)
    keep = np.ones(len(hist), dtype=bool)
    dropped = None
    for i in np.flatnonzero((dates >= today).to_numpy()):
        cap = _captured(hist["captured_at"].iloc[i]) if "captured_at" in hist.columns else None
        if dates.iloc[i] > today:
            reason = "dated after today"
        elif cap is not None:
            if cap >= close_at:
                continue
            when = f"{cap:%H:%M}" if cap.date() == now.date() else f"{cap:%Y-%m-%d %H:%M}"
            reason = f"captured {when} IST, before the 15:30 close"
        elif now >= close_at:
            continue
        else:
            reason = f"no capture time and it is {now:%H:%M} IST, before the 15:30 close"
        keep[i] = False
        dropped = {"date": dates.iloc[i].date().isoformat(), "reason": reason}
    if dropped is None:
        return hist, None
    return hist[keep], dropped


def _archive(symbol: str, root) -> tuple[pd.DataFrame, str]:
    cands = [symbol]
    alias = INDEX_ALIASES.get(symbol)
    if alias:
        cands.append(alias.replace("^", "_").replace("=", "_").replace("/", "_"))
    for c in cands:
        f = _store(root) / "history" / f"{c}.parquet"
        if f.exists():
            df = pd.read_parquet(f)
            df["date"] = pd.to_datetime(df["date"])
            return df.sort_values("date").reset_index(drop=True), f.name
    raise DataError(f"no archived history for {symbol} (tried {cands})")


def _provider_bars(provider, symbol, after: date | None, upto: date, now) -> pd.DataFrame:
    raw = provider.history(symbol, period="1mo", interval="1d")
    idx = pd.to_datetime(raw.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    cols = {str(c).lower(): c for c in raw.columns}
    bars = pd.DataFrame({k: pd.to_numeric(raw[cols[k]], errors="coerce").to_numpy()
                         for k in ("open", "high", "low", "close") if k in cols})
    bars.insert(0, "date", idx.normalize())
    bars, _ = settled_bars(bars.dropna(subset=["close"]), now)
    sel = bars["date"] <= pd.Timestamp(upto)
    if after is not None:
        sel &= bars["date"] > pd.Timestamp(after)
    return bars[sel]


def _official(symbol, days, root) -> pd.DataFrame:
    """NSE's ind_close_all bars already on disk for these days; a missing day is skipped."""
    rows = []
    if symbol in index_bars.NAMES:
        for d in days:
            try:
                b = index_bars.bar(d, symbol, _store(root))
            except DataError:
                continue
            rows.append({"date": pd.Timestamp(b["date"]), **{k: b[k] for k in ("open", "high", "low", "close")}})
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])


def _span(df) -> str:
    return f"{df['date'].iloc[0].date()}..{df['date'].iloc[-1].date()}"


def _fill(hist, symbol, upto: date | None, now, provider, root) -> tuple[pd.DataFrame, str, dict]:
    """Bars after the archive's last bar up to `upto`, in memory and never written: NSE's official
    ind_close_all files on disk first, then the provider for any weekday they do not cover."""
    last = hist["date"].iloc[-1].date() if len(hist) else None
    if upto is None or (last is not None and last >= upto):
        return hist, "", {}
    days = [d.date() for d in pd.bdate_range(last + timedelta(days=1) if last else upto, upto)]
    nse = _official(symbol, days, root)
    more, extra, fills = "", {}, [nse]
    if len(nse):
        more += f" + NSE ind_close_all {_span(nse)}"
    if len(nse) < len(days) and not is_offline():
        try:
            if provider is None:
                from shunkan.data.provider import get_provider

                provider = get_provider()
            prov = _provider_bars(provider, symbol, last, upto, now)
            prov = prov[~prov["date"].isin(nse["date"])]
            if len(prov):
                fills.append(prov)
                more += f" + provider bars {_span(prov)} (in memory, not written)"
        except Exception as exc:
            extra["fill_error"] = _msg(exc)
    fills = [f for f in fills if len(f)]
    if not fills:
        return hist, more, extra
    return pd.concat([hist, *fills], ignore_index=True).sort_values("date"), more, extra


def _vix(root, now, upto: date, provider=None) -> dict:
    src = "history archive _INDIAVIX.parquet"
    try:
        f = _store(root) / "history" / "_INDIAVIX.parquet"
        if not f.exists():
            raise DataError("no _INDIAVIX.parquet in the history archive")
        v = pd.read_parquet(f)
        v["date"] = pd.to_datetime(v["date"])
        v, _ = settled_bars(v.sort_values("date"), now)
        v = v[v["date"] <= pd.Timestamp(upto)].dropna(subset=["close"])
        v, more, extra = _fill(v, "INDIAVIX", upto, now, provider, root)
        src += more
        if v.empty:
            raise DataError(f"no VIX bar on or before {upto}")
        last = float(v["close"].iloc[-1])
        return {"close": last, "as_of": v["date"].iloc[-1].date().isoformat(),
                "pctile": float((v["close"] < last).mean() * 100),
                "since": v["date"].iloc[0].date().isoformat(), "source": src, **extra}
    except Exception as exc:
        return {"error": _msg(exc), "source": src}


def _price(symbol, as_of, now, root, provider) -> dict:
    hist, name = _archive(symbol, root)
    hist, dropped = settled_bars(hist, now)
    hist, more, extra = _fill(hist, symbol, as_of, now, provider, root)
    source = f"history archive {name}" + more
    if as_of is not None:
        hist = hist[hist["date"] <= pd.Timestamp(as_of)]
    if len(hist) < 2:
        raise DataError(f"{name}: fewer than two settled bars")
    row, prev = hist.iloc[-1], hist.iloc[-2]
    close = hist.set_index("date")["close"].astype(float)
    r = np.log(close).diff().dropna()

    def rv(n):
        return float(r.tail(n).std() * math.sqrt(252) * 100) if len(r) >= n else None

    d = row["date"].date()
    pc = float(prev["close"])
    out = {"as_of": d.isoformat(), "source": source, "close": float(row["close"]),
           "prev_close": pc, "prev_date": prev["date"].date().isoformat(),
           "change": float(row["close"]) - pc, "change_pct": (float(row["close"]) / pc - 1) * 100,
           "open": float(row["open"]), "gap_pct": (float(row["open"]) / pc - 1) * 100,
           "high": float(row["high"]), "low": float(row["low"]),
           "range_pct": (float(row["high"]) - float(row["low"])) / pc * 100,
           "rv5": rv(5), "rv21": rv(21), "vix": _vix(root, now, d, provider), **extra}
    if dropped:
        out["unsettled_bar_dropped"] = dropped
    if as_of is not None and d < as_of:
        out["stale"] = True
        out["note"] = f"last settled bar is {d}; session {as_of} is not in the archive"
    return out


def _hist_close(symbol, day: date, root) -> float | None:
    hist, _ = _archive(symbol, root)
    hist, _ = settled_bars(hist, datetime.combine(day, time(23, 59), IST))
    hit = hist[hist["date"] == pd.Timestamp(day)]
    return float(hit["close"].iloc[-1]) if len(hit) else None


def _side(front: pd.DataFrame, right: str) -> pd.DataFrame:
    return (front[front["right"] == right]
            .groupby("strike").agg(oi=("oi", "sum"), oi_change=("oi_change", "sum"),
                                   settle=("settle", "last")))


def _pcr(chain: pd.DataFrame) -> float | None:
    ce = chain.loc[chain["right"] == "CE", "oi"].sum()
    pe = chain.loc[chain["right"] == "PE", "oi"].sum()
    return float(pe / ce) if ce > 0 else None


def _lots(units, lot):
    if not lot:
        return None
    x = float(units) / lot
    return int(x) if x.is_integer() else round(x, 1)


def _adds(side: pd.DataFrame, lot) -> list[dict]:
    top = side[side["oi_change"] > 0].sort_values("oi_change", ascending=False, kind="stable").head(3)
    return [{"strike": float(k), "oi_change": int(v), "lots": _lots(v, lot)}
            for k, v in top["oi_change"].items()]


def _wall(side: pd.DataFrame, spot, right: str) -> tuple[float | None, str | None]:
    if not spot or spot != spot:
        return None, "no spot, so no out-of-the-money side"
    word, where = ("call", "above") if right == "CE" else ("put", "below")
    oi = side["oi"][side.index >= spot] if right == "CE" else side["oi"][side.index <= spot]
    if oi.sum() <= 0:
        return None, f"no {word} strike at or {where} spot {spot}"
    return float(oi.idxmax()), None


def _positioning(df, sym, as_of: date, for_session: date, root, spot, spot_source) -> dict:
    opts = df[(df["kind"] == "OPTIDX") & (df["symbol"] == sym) & (df["expiry"] > pd.Timestamp(as_of))]
    if opts.empty:
        raise DataError(f"no {sym} options expiring after {as_of} in the bhavcopy")
    exps = sorted(opts["expiry"].unique())
    front = pd.Timestamp(exps[0])
    nxt = pd.Timestamp(exps[1]) if len(exps) > 1 else None
    fr = opts[opts["expiry"] == front]
    ce, pe = _side(fr, "CE"), _side(fr, "PE")
    lots = fr["lot"].dropna()
    lot = float(lots.mode().iloc[0]) if len(lots) else None
    lot = int(lot) if lot is not None and lot.is_integer() else lot
    strikes = np.array(sorted(set(ce.index) | set(pe.index)), dtype=float)
    coi = ce["oi"].reindex(strikes, fill_value=0).to_numpy(dtype=float)
    poi = pe["oi"].reindex(strikes, fill_value=0).to_numpy(dtype=float)

    if spot is not None and spot != spot:
        spot = None
    if spot is None:
        spot_source = None
        try:
            spot = _hist_close(sym, as_of, root)
            if spot is not None:
                spot_source = f"history close {as_of}"
        except Exception:
            spot = None
        if spot is None:
            und = df.loc[df["symbol"] == sym, "underlying"].dropna()
            if len(und):
                spot, spot_source = float(und.median()), "bhavcopy underlying (median)"

    both = sorted(set(ce.index) & set(pe.index))
    atm = straddle = implied = None
    if spot and both:
        atm = min(both, key=lambda k: (abs(k - spot), k))
        straddle = float(ce.at[atm, "settle"] + pe.at[atm, "settle"])
        implied = straddle / spot * 100

    futs = df[(df["kind"] == "FUTIDX") & (df["symbol"] == sym)
              & (df["expiry"] > pd.Timestamp(as_of))].sort_values("expiry")
    fut_exp = fut_close = basis = None
    if len(futs):
        fut_exp, fut_close = futs["expiry"].iloc[0].date(), float(futs["close"].iloc[0])
        basis = fut_close - spot if spot else None

    call_wall, call_note = _wall(ce, spot, "CE")
    put_wall, put_note = _wall(pe, spot, "PE")
    expiry_today = for_session == front.date()
    out = {"as_of": as_of.isoformat(), "source": _bhav_src(as_of), "symbol": sym,
           "for_session": for_session.isoformat(), "front_expiry": front.date().isoformat(),
           "next_expiry": nxt.date().isoformat() if nxt is not None else None, "lot": lot,
           "pcr_front": _pcr(fr), "pcr_next": _pcr(opts[opts["expiry"] == nxt]) if nxt is not None else None,
           "max_pain": _max_pain(strikes, coi, poi),
           "call_wall": call_wall, "put_wall": put_wall,
           "call_oi_adds": _adds(ce, lot), "put_oi_adds": _adds(pe, lot),
           "spot": spot, "spot_source": spot_source, "atm_strike": atm, "straddle": straddle,
           "implied_move_pct": implied, "future_expiry": fut_exp, "future_close": fut_close,
           "basis": basis, "expiry_today": expiry_today}
    if call_note:
        out["call_wall_note"] = call_note
    if put_note:
        out["put_wall_note"] = put_note
    if expiry_today:
        out["warning"] = EXPIRY_WARNING
    return out


def eod_positioning(symbol: str, as_of: date, for_session: date, root=None,
                    spot: float | None = None) -> dict:
    sym, a, f = symbol.upper(), _day(as_of), _day(for_session)
    df = fo_bhav.load(a, symbols=[sym], kinds=["OPTIDX", "FUTIDX"], root=root)
    return _plain(_positioning(df, sym, a, f, root, spot, "given" if spot is not None else None))


def _sessions_between(start: date, end: date, hols) -> int:
    n, d = 0, start
    while d <= end:
        n += cal.is_trading_day(d, hols)
        d += timedelta(days=1)
    return n


def _expiry(df, sym, as_of: date, for_session: date, hols) -> dict:
    rows = {}
    for s in dict.fromkeys([sym, "NIFTY", "BANKNIFTY"]):
        exps = sorted({e for e in df.loc[df["symbol"] == s, "expiry"].dt.date if e > as_of})
        if not exps:
            rows[s] = {"error": f"no {s} contract expiring after {as_of} in the bhavcopy",
                       "source": _bhav_src(as_of)}
            continue
        rows[s] = {"next": exps[0].isoformat(),
                   "sessions_left": _sessions_between(for_session, exps[0], hols),
                   "expiry_today": exps[0] == for_session}
    if "error" in rows[sym]:
        raise DataError(rows[sym]["error"])
    out = {"as_of": as_of.isoformat(), "source": _bhav_src(as_of),
           "for_session": for_session.isoformat(), "symbols": rows,
           "expiry_today": rows[sym]["expiry_today"]}
    if out["expiry_today"]:
        out["warning"] = EXPIRY_WARNING
    return out


def _latest(as_of: date, hols, have, what: str) -> date:
    d = as_of
    for _ in range(LOOKBACK):
        if have(d):
            return d
        d = cal.prev_session(d, hols)
    raise DataError(f"{what} not on disk for {as_of} or the {LOOKBACK - 1} sessions before")


def _mark_stale(item: dict, d: date, as_of: date, what: str) -> dict:
    if d != as_of:
        item["stale"] = True
        item["note"] = f"still on {d}; {what} for {as_of} not on disk"
    return item


def _participant(as_of, hols, root) -> dict:
    found: dict = {}
    held: dict = {}
    bad: list[str] = []

    def held_rows():
        if "rows" not in held:
            path = _store(root) / "participant" / "fao_oi.parquet"
            held["rows"] = pd.DataFrame({"date": pd.Series(dtype="datetime64[ns]")})
            try:
                if path.exists():
                    rows = pd.read_parquet(path)
                    rows["date"] = pd.to_datetime(rows["date"]).dt.normalize()
                    held["rows"] = rows
            except Exception as exc:
                held["error"] = f"participant store unreadable: {_msg(exc)}"
        return held["rows"]

    def have(d):
        if d not in found:
            found[d] = None
            raw = participant.raw_path(d, root)
            if raw.exists():
                try:
                    found[d] = (participant.day_table(d, root), f"NSE {raw.name}", "raw")
                except Exception as exc:
                    bad.append(f"{raw.name} unreadable: {_msg(exc)}")
            if found[d] is None:
                rows = held_rows()
                rows = rows[rows["date"] == pd.Timestamp(d)].drop_duplicates("client_type", keep="last")
                found[d] = (rows, PART_STORE_SRC, "store") if len(rows) else None
        return found[d] is not None

    def missing(msg):
        return DataError("; ".join([msg, *bad, *([held["error"]] if "error" in held else [])]))

    try:
        d = _latest(as_of, hols, have, "participant OI")
    except DataError as exc:
        raise missing(str(exc)) from None
    p = cal.prev_session(d, hols)
    if not have(p):
        raise missing(f"participant OI for {p} not on disk, so no change for {d}")
    (cur, cur_src, cur_kind), (prev, prev_src, prev_kind) = found[d], found[p]
    source = cur_src if cur_kind == prev_kind else f"{cur_src}; {p} from {prev_src}"
    out = {"as_of": d.isoformat(), "source": source, "unit": "contracts",
           **participant._change(cur, prev, d, p)}
    return _mark_stale(out, d, as_of, "participant OI")


def _fii_futures(as_of, hols, root) -> dict:
    def have(d):
        return flows.fii_stats_path(d, root).exists()

    d = _latest(as_of, hols, have, "fii_stats file")
    cur = flows.fii_derivatives(d, root)["index_futures"]
    out = {"as_of": d.isoformat(), "source": f"NSE {flows.fii_stats_path(d, root).name}",
           **{k: cur[k] for k in ("buy_cr", "sell_cr", "net_cr", "oi_contracts")},
           "oi_change_contracts": None}
    p = cal.prev_session(d, hols)
    if not have(p):
        out["oi_change_note"] = f"fii_stats for {p} not on disk"
    else:
        try:
            out["oi_change_contracts"] = cur["oi_contracts"] - flows.fii_derivatives(p, root)["index_futures"]["oi_contracts"]
            out["prev_date"] = p.isoformat()
        except DataError as exc:
            out["oi_change_note"] = f"fii_stats for {p}: {exc}"
    return _mark_stale(out, d, as_of, "fii_stats")


def _iso(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _cash(as_of, root) -> dict:
    got = flows.fii_dii_cash_on(as_of, root)
    stale = got is None
    if stale:
        folder = flows.fiidii_path(as_of, root).parent.parent
        days = sorted(d for p in folder.glob("*/*.json") if (d := _iso(p.stem)) and d <= as_of)
        if not days:
            raise DataError(f"no FII/DII cash figures cached on or before {as_of}")
        got = flows.fii_dii_cash_on(days[-1], root)
    out = {"as_of": got["date"], "source": CASH_SRC, "fii": got["fii"], "dii": got["dii"],
           "basis": got["basis"]}
    if stale:
        out["stale"] = True
        out["note"] = f"{as_of} not cached; newest cached on or before it is {got['date']}"
    return out


def _flows(as_of, hols, root) -> dict:
    return {"as_of": as_of.isoformat(), "source": FLOWS_SRC,
            "participant": _guard(PART_SRC, lambda: _participant(as_of, hols, root)),
            "fii_index_futures": _guard(FII_SRC, lambda: _fii_futures(as_of, hols, root)),
            "cash": _guard(CASH_SRC, lambda: _cash(as_of, root))}


def _hols(now, root) -> tuple[list[date], list[str]]:
    notes = []
    try:
        hols = cal.holidays(root=root, now=now, fetch=False)
    except Exception as exc:
        return [], [f"{_msg(exc)}; every weekday is treated as a session"]
    fetched = cal.holidays_fetched_at(root)
    if fetched is not None and now - fetched >= timedelta(days=7):
        notes.append(f"holiday list fetched {fetched:%Y-%m-%d}, older than 7 days")
    return hols, notes


def phase_of(now: datetime, hols) -> str:
    now = _ist(now)
    if not cal.is_trading_day(now.date(), hols):
        return "closed"
    if now.time() < OPEN:
        return "pre_open"
    return "open" if now.time() < CLOSE else "post_close"


def _session(now, root, hols, notes) -> dict:
    today = now.date()
    days = fo_bhav.sessions(end=today, root=root)
    if not days:
        raise DataError("no F&O bhavcopy on disk")
    as_of = days[-1]
    for_session = cal.next_session(as_of, hols)
    warnings = list(notes)
    year = cal.holidays_year(root)
    if year is not None and for_session.year != year:
        warnings.append(f"holiday list covers {year}; {for_session.year} holidays are not known yet")
    last = cal.prev_session(today, hols)
    if as_of < last:
        warnings.append(f"latest bhavcopy on disk is {as_of}; {last} is missing")
    phase = phase_of(now, hols)
    if phase == "post_close" and as_of < today:
        warnings.append(POST_CLOSE_WARNING.format(today))
    return {"as_of": as_of.isoformat(), "for_session": for_session.isoformat(),
            "now_ist": now.isoformat(timespec="seconds"), "phase": phase,
            "holidays_skipped": [h.isoformat() for h in hols
                                 if as_of < h < for_session and h.weekday() < 5],
            "holidays_year": year, "warnings": warnings, "source": SESSION_SRC}


def _calendar(now, root, www) -> dict:
    w = cal.window(now, 48, root=root, www=www, fetch=False)
    return {"as_of": w["from"], "source": CAL_SRC, **w}


def _compose(symbol, now, root, provider, www) -> dict:
    sym = str(symbol or "NIFTY").strip().upper()
    now = _now(now)
    out: dict = {"symbol": sym, "generated_at": now.isoformat(timespec="seconds")}
    hols, notes = _hols(now, root)
    out["session"] = _guard(SESSION_SRC, lambda: _session(now, root, hols, notes))
    as_of = _iso(out["session"].get("as_of") or "")
    for_session = _iso(out["session"].get("for_session") or "")
    out["price"] = _guard(PRICE_SRC, lambda: _price(sym, as_of, now, root, provider))

    loaded: dict = {}

    def need():
        if as_of is None:
            raise DataError(f"no session: {out['session'].get('error')}")
        return as_of

    def frame():
        need()
        if "df" not in loaded:
            try:
                loaded["df"] = fo_bhav.load(as_of, symbols=list(dict.fromkeys([sym, "NIFTY", "BANKNIFTY"])),
                                            kinds=["OPTIDX", "FUTIDX"], root=root)
            except Exception as exc:
                loaded["df"] = exc
        if isinstance(loaded["df"], Exception):
            raise loaded["df"]
        return loaded["df"]

    price = out["price"]
    spot = price.get("close") if as_of and price.get("as_of") == as_of.isoformat() else None
    out["positioning"] = _guard(_bhav_src(as_of), lambda: _positioning(
        frame(), sym, as_of, for_session, root, spot, f"price close {as_of}" if spot else None))
    out["flows"] = _guard(FLOWS_SRC, lambda: _flows(need(), hols, root))
    out["expiry"] = _guard(_bhav_src(as_of), lambda: _expiry(frame(), sym, as_of, for_session, hols))
    out["calendar"] = _guard(CAL_SRC, lambda: _calendar(now, root, www))
    return out


def compose(symbol: str = "NIFTY", now: datetime | None = None, root=None, provider=None,
            www=None) -> dict:
    try:
        return _plain(_compose(symbol, now, root, provider, www))
    except Exception as exc:
        err = {"error": _msg(exc), "source": "facts.compose"}
        return {"symbol": str(symbol), **{s: dict(err) for s in SECTIONS}}


def _status(exc: Exception) -> str:
    return f"unpublished: {exc}" if isinstance(exc, Unpublished) else f"failed: {_msg(exc)}"


def _attempt(fn) -> str:
    try:
        fn()
        return "ok"
    except Exception as exc:
        return _status(exc)


def _cash_for(session: date, root, www) -> None:
    if flows.fii_dii_cash_on(session, root) is not None:
        return
    got = flows.fii_dii_cash(root, www)
    if got["date"] < session.isoformat():
        raise Unpublished(f"latest is {got['date']}")


BACKFILL = 5  # earlier sessions whose missing bhavcopy each refresh fetches, so a few days off leave no gap


def refresh(now: datetime | None = None, root=None, client=None, www=None) -> dict:
    now = _now(now)
    sources: dict[str, str] = {}
    hols: list[date] = []
    try:
        rows, note = cal._holidays(root, www, 7, now, True)
        hols = [d for d, _ in rows]
        sources["holidays"] = f"failed: {note}" if note else "ok"
    except Exception as exc:
        sources["holidays"] = _status(exc)
    today = now.date()
    session = (today if cal.is_trading_day(today, hols) and now.time() >= CLOSE
               else cal.prev_session(today, hols))
    prev = cal.prev_session(session, hols)
    sources["bhavcopy"] = _attempt(lambda: fo_bhav.fetch(session, root, client))
    back = session
    for _ in range(BACKFILL):
        back = cal.prev_session(back, hols)
        if fo_bhav.path_for(back, root) is None:
            _attempt(lambda d=back: fo_bhav.fetch(d, root, client))
    sources["index_close"] = _attempt(lambda: index_bars.fetch(session, root, client))
    sources["participant"] = _attempt(lambda: participant.day_table(session, root, client))
    _attempt(lambda: participant.day_table(prev, root, client))
    sources["fii_derivatives"] = _attempt(lambda: flows.fii_derivatives(session, root, client))
    _attempt(lambda: flows.fii_derivatives(prev, root, client))
    sources["fii_dii_cash"] = _attempt(lambda: _cash_for(session, root, www))
    sources["board_meetings"] = _attempt(lambda: cal.board_meetings(
        today, (now + timedelta(hours=48)).date(), root, www, now, fetch=True))
    sources["universe"] = _attempt(lambda: cal.index_universe(root, True, now))
    _attempt(lambda: fo_store.build(root, workers=1, create=False))
    _attempt(lambda: fo_iv.build(root, workers=1, create=False))
    return {"session": session.isoformat(), "sources": sources,
            "complete": all(v == "ok" for v in sources.values())}
