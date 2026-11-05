"""FII flows NSE publishes each evening.

fii_derivatives: the archive's fii_stats .xls, FII buy/sell/OI in contracts
and Rs crore per F&O segment. fii_dii_cash: the www endpoint's provisional
FII/FPI and DII cash-market figures (NSE+BSE+MSEI); it serves only the latest
session, so each payload is cached under its own date. Raw bodies are cached
write-once, and only after they parse.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pandas as pd

from shunkan.data import nse_archive, nse_www
from shunkan.data.provider import FetchFailed

FII_STATS_URL = nse_archive.ARCHIVE + "/content/fo/fii_stats_{dmy}.xls"
FIIDII_PATH = "/api/fiidiiTradeReact"
BASIS = "NSE provisional, NSE+BSE+MSEI"
_ROWS = {"INDEX FUTURES": "index_futures", "NIFTY FUTURES": "nifty_futures",
         "BANKNIFTY FUTURES": "banknifty_futures", "INDEX OPTIONS": "index_options",
         "STOCK FUTURES": "stock_futures", "STOCK OPTIONS": "stock_options"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DMY = re.compile(r"\b(\d{1,2})-([A-Za-z]{3})-(\d{4})\b")


def _day(day) -> date:
    return pd.Timestamp(day).date()


def _dmy(d: date) -> str:
    return f"{d:%d}-{_MONTHS[d.month - 1]}-{d:%Y}"


def _find_date(text: str) -> date | None:
    m = _DMY.search(text)
    if not m or m.group(2).title() not in _MONTHS:
        return None
    return date(int(m.group(3)), _MONTHS.index(m.group(2).title()) + 1, int(m.group(1)))


def _num(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    return float(str(v).replace(",", "").strip())


def _count(v) -> int:
    f = _num(v)
    if not f.is_integer():
        raise ValueError(f"{v!r} is not a contract count")
    return int(f)


def _base(root) -> Path:
    if root is None:
        from shunkan.store.store import STORE_DIR

        return STORE_DIR
    return Path(root)


def parse_fii_stats(data: bytes, day) -> dict:
    d = _day(day)
    try:
        return _fii_stats(_rows(data), d)
    except ImportError:
        raise
    except Exception as exc:
        raise FetchFailed(f"fii_stats_{_dmy(d)}.xls: {exc}") from exc


def _rows(data: bytes) -> list[list]:
    import xlrd

    sheet = xlrd.open_workbook(file_contents=data).sheet_by_index(0)
    return [[str(v).strip() if isinstance(v, str) else v for v in sheet.row_values(r)]
            for r in range(sheet.nrows)]


def _norm(v) -> str:
    return " ".join(str(v).upper().split())


def _fii_stats(rows: list[list], d: date) -> dict:
    head = [_norm(v) for v in rows[1][1:6]] if len(rows) > 1 else []
    if len(head) < 5 or head[:4] != ["BUY", "BUY", "SELL", "SELL"] \
            or not head[4].startswith("OPEN INTEREST"):
        raise ValueError(f"fii_stats header changed: {head}")
    title = next((" ".join(str(v) for v in r if v != "") for r in rows if any(v != "" for v in r)), "")
    named = _find_date(title)
    if named is not None and named != d:
        raise ValueError(f"title {title!r} names another day")
    out = {"date": d.isoformat()}
    for label, key in _ROWS.items():
        found = [r for r in rows if r and _norm(r[0]) == label]
        if len(found) != 1:
            raise ValueError(f"{len(found)} {label!r} rows")
        r = found[0]
        buy_cr, sell_cr = _num(r[2]), _num(r[4])
        out[key] = {"buy_contracts": _count(r[1]), "buy_cr": buy_cr,
                    "sell_contracts": _count(r[3]), "sell_cr": sell_cr,
                    "net_cr": round(buy_cr - sell_cr, 2),
                    "oi_contracts": _count(r[5]), "oi_cr": _num(r[6])}
    return out


def fii_stats_path(day, root=None) -> Path:
    d = _day(day)
    return _base(root) / "flows" / "fii_stats" / f"{d:%Y}" / f"fii_stats_{_dmy(d)}.xls"


def fii_derivatives(day, root=None, client=None) -> dict:
    d = _day(day)
    path = fii_stats_path(d, root)
    if path.exists():
        return parse_fii_stats(path.read_bytes(), d)
    data = nse_archive.get(FII_STATS_URL.format(dmy=_dmy(d)), "xls", client=client)
    out = parse_fii_stats(data, d)
    nse_archive.save_once(path, data)
    return out


def parse_fiidii(js) -> dict:
    if not isinstance(js, list) or not all(isinstance(r, dict) for r in js):
        raise FetchFailed(f"fiidiiTradeReact: expected a list of rows, got {str(js)[:80]}")
    out, days = {}, set()
    for cat, key in (("FII/FPI", "fii"), ("DII", "dii")):
        found = [r for r in js if _norm(str(r.get("category", "")).replace("*", "")) == cat]
        if len(found) != 1:
            raise FetchFailed(f"fiidiiTradeReact: {len(found)} {cat} rows")
        r = found[0]
        try:
            out[key] = {"buy_cr": _num(r["buyValue"]), "sell_cr": _num(r["sellValue"]),
                        "net_cr": _num(r["netValue"])}
        except (KeyError, ValueError) as exc:
            raise FetchFailed(f"fiidiiTradeReact {cat}: {type(exc).__name__}: {exc}") from exc
        day = _find_date(str(r.get("date", "")))
        if day is None:
            raise FetchFailed(f"fiidiiTradeReact {cat}: unreadable date {r.get('date')!r}")
        days.add(day)
    if len(days) != 1:
        raise FetchFailed(f"fiidiiTradeReact: FII and DII date disagree {sorted(map(str, days))}")
    return {"date": days.pop().isoformat(), **out, "basis": BASIS}


def fiidii_path(day, root=None) -> Path:
    d = _day(day)
    return _base(root) / "flows" / "fiidii" / f"{d:%Y}" / f"{d.isoformat()}.json"


def fii_dii_cash(root=None, www=None) -> dict:
    js = (www or nse_www).get_json(FIIDII_PATH)
    path = nse_archive.save_once(fiidii_path(parse_fiidii(js)["date"], root),
                                 json.dumps(js).encode())
    return parse_fiidii(json.loads(path.read_bytes()))


def fii_dii_cash_on(day, root=None) -> dict | None:
    path = fiidii_path(day, root)
    if not path.exists():
        return None
    return parse_fiidii(json.loads(path.read_bytes()))
