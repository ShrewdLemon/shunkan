"""What falls inside the next 48 hours: expiries, F&O holidays, index
constituents' board meetings, and RBI, FOMC and CPI dates.

Holidays and board meetings come from www.nseindia.com through nse_www and are
cached under <store>/calendar/ with the constituent symbols that filter the
board meetings, each cache replaced whole, never edited in place. fetch=False
reads only those caches, labelling stale or missing ones, so a read path never
touches NSE. Macro dates are a packaged transcription
with a coverage end per series; past it the series is reported stale, never
guessed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from datetime import date, datetime, time, timedelta
from importlib import resources
from pathlib import Path

from shunkan.data import fo_bhav, nse_www
from shunkan.data.provider import DataError, FetchFailed
from shunkan.markets import IST

HOLIDAYS_PATH = "/api/holiday-master"
BOARD_PATH = "/api/corporate-board-meetings"
BOARD_TTL = timedelta(hours=6)
OPEN, EXPIRY = time(9, 15), time(15, 30)
SERIES = {"RBI MPC decision": "rbi_mpc", "FOMC decision": "fomc",
          "US CPI": "us_cpi", "India CPI": "india_cpi"}
_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DMY = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
_BOARD_FILE = re.compile(r"\d{8}\.json")


def _base(root) -> Path:
    if root is None:
        from shunkan.store.store import STORE_DIR

        return STORE_DIR
    return Path(root)


def _ist(dt: datetime) -> datetime:
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def _now(now) -> datetime:
    return datetime.now(IST) if now is None else _ist(now)


def _read(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _replace(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _nse_date(s, what: str) -> date:
    m = _DMY.fullmatch(str(s).strip())
    if m and m.group(2).title() in _MON:
        try:
            return date(int(m.group(3)), _MON.index(m.group(2).title()) + 1, int(m.group(1)))
        except ValueError:
            pass
    raise FetchFailed(f"{what}: unreadable date {s!r}")


def _holiday_rows(js) -> list[tuple[date, str]]:
    rows = js.get("FO") if isinstance(js, dict) else None
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        raise FetchFailed(f"holiday-master: no FO rows in {str(js)[:80]}")
    return sorted((_nse_date(r.get("tradingDate"), "holiday-master"),
                   str(r.get("description") or "").strip()) for r in rows)


def _cached_holidays(root):
    js = _read(_base(root) / "calendar" / "holidays.json")
    try:
        return _ist(datetime.fromisoformat(js["fetched_at"])), _holiday_rows(js["payload"])
    except (KeyError, TypeError, ValueError, FetchFailed):
        return None, None


def _holidays(root, www, max_age_days, now, fetch=True) -> tuple[list[tuple[date, str]], str | None]:
    now = _now(now)
    fetched, rows = _cached_holidays(root)
    if rows is not None and now - fetched < timedelta(days=max_age_days):
        return rows, None
    if not fetch:
        if rows is None:
            raise DataError("holidays: not cached yet")
        return rows, f"holiday list fetched {fetched:%Y-%m-%d}, older than {max_age_days} days"
    try:
        js = (www or nse_www).get_json(HOLIDAYS_PATH, {"type": "trading"})
        fresh = _holiday_rows(js)
    except DataError as exc:
        if rows is None:
            raise
        return rows, f"holiday list fetched {fetched:%Y-%m-%d}; refresh failed: {exc}"
    _replace(_base(root) / "calendar" / "holidays.json",
             {"fetched_at": now.isoformat(), "payload": js})
    return fresh, None


def holidays(root=None, www=None, max_age_days: int = 7, now=None, fetch: bool = True) -> list[date]:
    return [d for d, _ in _holidays(root, www, max_age_days, now, fetch)[0]]


def holidays_fetched_at(root=None) -> datetime | None:
    return _cached_holidays(root)[0]


def holidays_year(root=None) -> int | None:
    rows = _cached_holidays(root)[1]
    return Counter(d.year for d, _ in rows).most_common(1)[0][0] if rows else None


def is_trading_day(d: date, hols) -> bool:
    return d.weekday() < 5 and d not in hols


def next_session(after: date, hols) -> date:
    d = after + timedelta(days=1)
    while not is_trading_day(d, hols):
        d += timedelta(days=1)
    return d


def prev_session(before: date, hols) -> date:
    d = before - timedelta(days=1)
    while not is_trading_day(d, hols):
        d -= timedelta(days=1)
    return d


def _board_rows(js) -> list[dict]:
    if not isinstance(js, list) or not all(isinstance(r, dict) for r in js):
        raise FetchFailed(f"corporate-board-meetings: expected a list of rows, got {str(js)[:80]}")
    merged: dict[tuple[date, str], list[str]] = {}
    for r in js:
        sym = str(r.get("bm_symbol") or "").strip()
        if not sym or "bm_date" not in r:
            raise FetchFailed(f"corporate-board-meetings: row without symbol or date {str(r)[:80]}")
        purposes = merged.setdefault((_nse_date(r["bm_date"], "corporate-board-meetings"), sym), [])
        p = str(r.get("bm_purpose") or "").strip()
        if p and p not in purposes:
            purposes.append(p)
    return [{"symbol": s, "date": d, "purpose": "; ".join(ps)}
            for (d, s), ps in sorted(merged.items())]


def _board_cache(path: Path):
    js = _read(path)
    try:
        return (_ist(datetime.fromisoformat(js["fetched_at"])), date.fromisoformat(js["from"]),
                date.fromisoformat(js["to"]), _board_rows(js["payload"]))
    except (KeyError, TypeError, ValueError, FetchFailed):
        return None


def _board(start, end, root, www, now, fetch) -> tuple[list[dict], str | None]:
    now = _now(now)
    folder = _base(root) / "calendar" / "board"
    today = folder / f"{now:%Y%m%d}.json"
    if fetch:
        hit = _board_cache(today)
        if hit and now - hit[0] < BOARD_TTL and hit[1] <= start and hit[2] >= end:
            rows = hit[3]
        else:
            js = (www or nse_www).get_json(BOARD_PATH, {"index": "equities",
                                                        "from_date": f"{start:%d-%m-%Y}",
                                                        "to_date": f"{end:%d-%m-%Y}"})
            rows = _board_rows(js)
            _replace(today, {"fetched_at": now.isoformat(), "from": start.isoformat(),
                             "to": end.isoformat(), "payload": js})
        return [r for r in rows if start <= r["date"] <= end], None
    names = sorted((p.name for p in folder.glob("*.json")
                    if _BOARD_FILE.fullmatch(p.name) and p.name <= today.name), reverse=True)
    hit = next((h for n in names if (h := _board_cache(folder / n))), None)
    if hit is None:
        raise DataError("board meetings: not cached yet")
    fetched, lo, hi, rows = hit
    notes = []
    if now - fetched >= BOARD_TTL:
        notes.append(f"board meetings fetched {fetched:%Y-%m-%d %H:%M} IST")
    if lo > start or hi < end:
        notes.append(f"cached for {lo}..{hi} only, asked {start}..{end}")
    return [r for r in rows if start <= r["date"] <= end], "; ".join(notes) or None


def board_meetings(start: date, end: date, root=None, www=None, now=None,
                   fetch: bool = True) -> list[dict]:
    return _board(start, end, root, www, now, fetch)[0]


def index_universe(root=None, fetch: bool = True, now=None) -> set[str]:
    from shunkan.data import constituents

    path = _base(root) / "calendar" / "universe.json"
    if fetch:
        syms = sorted({c.symbol for c in constituents.universe()})
        day = _now(now).date().isoformat()
        saved = _read(path)
        if not (isinstance(saved, dict) and saved.get("date") == day and saved.get("symbols") == syms):
            _replace(path, {"date": day, "symbols": syms})
        return set(syms)
    members = constituents.cached_universe()
    if members is not None:
        return {c.symbol for c in members}
    saved = _read(path)
    if isinstance(saved, dict) and isinstance(saved.get("symbols"), list) and saved["symbols"]:
        return {str(s) for s in saved["symbols"]}
    raise DataError("universe not cached")


def load_macro() -> dict:
    text = resources.files("shunkan.data").joinpath("macro_calendar.json").read_text(encoding="utf-8")
    return json.loads(text)


def macro_events(start: datetime, end: datetime) -> dict:
    start, end = _ist(start), _ist(end)
    data = load_macro()
    cov = {s: max(date.fromisoformat(d) for d in _ISO.findall(v))
           for s, v in data["coverage"].items()}
    found = []
    for e in data["events"]:
        at = datetime.combine(date.fromisoformat(e["date"]), time.fromisoformat(e["time_ist"]), IST)
        if start <= at <= end:
            found.append((at, e))
    found.sort(key=lambda x: x[0])
    rows = [{"at_ist": at.isoformat(), "series": SERIES[e["event"]], "event": e["event"],
             "detail": e["detail"], "source": e["source"]} for at, e in found]
    return {"rows": rows, "coverage": {s: d.isoformat() for s, d in cov.items()},
            "stale": sorted(s for s, d in cov.items() if d < end.date())}


def expiries(as_of: date, symbols=("NIFTY", "BANKNIFTY"), root=None) -> dict[str, list[date]]:
    d = as_of.date() if isinstance(as_of, datetime) else as_of
    df = fo_bhav.load(d, symbols=list(symbols), root=root)
    return {s: sorted({e for e in df.loc[df["symbol"] == s.upper(), "expiry"].dt.date if e >= d})
            for s in symbols}


def _msg(exc: Exception) -> str:
    return str(exc) if isinstance(exc, DataError) else f"{type(exc).__name__}: {exc}"


def window(now: datetime, hours: int = 48, universe: set[str] | None = None,
           root=None, www=None, fetch: bool = True) -> dict:
    now = _ist(now)
    end = now + timedelta(hours=hours)
    found, errors = [], {}
    coverage, stale = {}, []

    def add(day, at, kind, what, source):
        when = datetime.combine(day, at, IST)
        if now <= when <= end:
            found.append((when, kind, what, source))

    hol_days: list[date] = []
    try:
        hols, note = _holidays(root, www, 7, now, fetch)
        hol_days = [d for d, _ in hols]
        if note:
            errors["holidays"] = note
        for d, desc in hols:
            if d.weekday() >= 5:
                continue
            add(d, OPEN, "holiday", f"NSE F&O holiday: {desc}" if desc else "NSE F&O holiday",
                "NSE holiday-master (F&O)")
    except Exception as exc:
        errors["holidays"] = _msg(exc)

    try:
        days = fo_bhav.sessions(end=now.date(), root=root)
        if not days:
            raise DataError("no F&O bhavcopy on disk")
        if days[-1] < prev_session(now.date(), hol_days):
            errors["expiries"] = f"bhavcopy on disk is {days[-1]}; expiries may be incomplete"
        for sym, exps in expiries(days[-1], root=root).items():
            for e in exps:
                add(e, EXPIRY, "expiry", f"{sym} expiry", f"NSE F&O bhavcopy {days[-1].isoformat()}")
    except Exception as exc:
        errors["expiries"] = _msg(exc)

    try:
        if universe is None:
            universe = index_universe(root, fetch, now)
        meetings, note = _board(now.date(), end.date(), root, www, now, fetch)
        if note:
            errors["board_meetings"] = note
        for m in meetings:
            if m["symbol"] in universe:
                add(m["date"], OPEN, "board_meeting", f"{m['symbol']} board meeting: {m['purpose']}",
                    "NSE corporate board meetings")
    except Exception as exc:
        errors["board_meetings"] = _msg(exc)

    try:
        macro = macro_events(now, end)
        coverage, stale = macro["coverage"], macro["stale"]
        for r in macro["rows"]:
            head = r["detail"].split(";")[0].strip()
            found.append((datetime.fromisoformat(r["at_ist"]), "macro",
                          f"{r['event']} ({head})" if head else r["event"], r["source"]))
    except Exception as exc:
        errors["macro"] = _msg(exc)

    found.sort(key=lambda x: x[:3])
    return {"from": now.isoformat(timespec="seconds"), "to": end.isoformat(timespec="seconds"),
            "rows": [{"at_ist": w.isoformat(), "kind": k, "what": t, "source": s}
                     for w, k, t, s in found],
            "errors": errors, "coverage": coverage, "stale": stale}
