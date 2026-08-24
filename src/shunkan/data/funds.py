"""Mutual funds: schemes, disclosed portfolios, and the stock-to-scheme join.

Shunkan's ownership work reads SEBI's shareholding pattern, which names
holders at the AMC level - "SBI Mutual Fund, 6.96%". That is the legal
owner, not the economic one. The economic owner is a SCHEME: a particular
fund with a particular mandate, manager and NAV, and it is the scheme
that buys and sells.

This module imports the scheme layer from the local mfresearch pipeline
(AMFI NAVs, disclosed monthly portfolios, AMFI's own cap bands, SEBI
stress tests) into Shunkan's own parquet store, so the terminal reads its
own data at runtime and does not depend on a sibling project being
present. Import once, or whenever the pipeline refreshes.

The join that makes it worth doing: given RELIANCE, which schemes hold
it, at what weight, worth how much. Holding names are resolved to NSE
symbols through AMFI's own classification list first (it carries both the
name and the NSE code), then through the pipeline's own map. Anything
unresolved is COUNTED and reported, never silently dropped - a join that
hides its misses is a join you cannot trust.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pandas as pd

from shunkan.data.provider import DataError

DEFAULT_SOURCE = Path.home() / "Projects" / "mfresearch"


def funds_dir(root=None) -> Path:
    from shunkan.store.store import STORE_DIR

    d = (root or STORE_DIR) / "funds"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _norm(name: str) -> str:
    """Holding names carry vendor suffixes ('Ordinary Shares', 'Ltd.') that
    the exchange lists do not. Strip to a comparable core."""
    n = str(name).upper()
    n = re.sub(r"\b(ORDINARY SHARES|EQUITY SHARES|SHARES|CLASS [A-Z]\b)", " ", n)
    n = re.sub(r"\b(LIMITED|LTD|PVT|PRIVATE|CORP|CORPORATION|INDIA|INC|PLC|CO)\b", " ", n)
    n = re.sub(r"[^A-Z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def import_from_pipeline(source: Path | None = None, root=None) -> dict:
    """Read the mfresearch stores into Shunkan's parquet store."""
    src = Path(source or DEFAULT_SOURCE)
    db = src / "pipeline" / "mf.db"
    hold_dir = src / "datadump" / "holdings"
    if not db.exists():
        raise DataError(f"no mf.db at {db} - point `source` at the mfresearch checkout")

    con = sqlite3.connect(str(db))
    schemes = pd.read_sql_query("select * from schemes", con)
    try:
        mcap = pd.read_sql_query(
            "select period, isin, name, nse, avg_mcap, band from stock_mcap", con)
    except Exception:
        mcap = pd.DataFrame(columns=["period", "isin", "name", "nse", "avg_mcap", "band"])
    try:
        smap = pd.read_sql_query("select holding, symbol from stock_map", con)
    except Exception:
        smap = pd.DataFrame(columns=["holding", "symbol"])
    try:
        ter = pd.read_sql_query("select * from ter", con)
    except Exception:
        ter = pd.DataFrame()
    con.close()

    # ---- name -> NSE symbol resolver -------------------------------------
    lookup: dict[str, str] = {}
    if not mcap.empty:
        latest = mcap.sort_values("period").drop_duplicates("isin", keep="last")
        for r in latest.itertuples():
            if r.nse and isinstance(r.nse, str):
                lookup.setdefault(_norm(r.name), r.nse.strip().upper())
    for r in smap.itertuples():
        if isinstance(r.symbol, str) and r.symbol.strip():
            lookup.setdefault(_norm(r.holding), r.symbol.strip().upper())

    # ---- holdings --------------------------------------------------------
    rows, scheme_rows = [], []
    unresolved: dict[str, int] = {}
    files = sorted(hold_dir.glob("*.json")) if hold_dir.exists() else []
    for f in files:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        isin = d.get("isin") or f.stem
        port = d.get("portfolio") or {}
        alloc = port.get("assetAllocation") or {}
        capw = port.get("marketCapWeightage") or {}
        conc = port.get("concentration") or {}
        scheme_rows.append({
            "isin": isin, "scheme_name": d.get("schemeName"),
            "fetched": d.get("fetched"),
            "managers": d.get("schemeFundManagers"),
            "equity_pct": _f(alloc.get("equityAllocation")),
            "debt_pct": _f(alloc.get("debtAllocation")),
            "cash_pct": _f(alloc.get("cashAllocation")),
            "largecap_pct": _f(capw.get("largeCap")),
            "midcap_pct": _f(capw.get("midCap")),
            "smallcap_pct": _f(capw.get("smallCap")),
            "n_holdings": conc.get("numberOfHoldings"),
            "top10_pct": _f(conc.get("top10StocksWeight")),
        })
        for h in (d.get("holdings") or []):
            nm = h.get("name")
            if not nm:
                continue
            sym = lookup.get(_norm(nm))
            if sym is None:
                unresolved[nm] = unresolved.get(nm, 0) + 1
            rows.append({
                "isin": isin, "scheme_name": d.get("schemeName"),
                "holding": nm, "symbol": sym, "sector": h.get("sector"),
                "market_value_cr": _f(h.get("marketValue")),
                "weight_pct": _f(h.get("weightage")),
                "change_1m_pct": _f(h.get("change1M")),
                "as_of": d.get("fetched"),
            })

    # ---- NAV and benchmark series ---------------------------------------
    con = sqlite3.connect(str(db))
    try:
        nav = pd.read_sql_query("select code, d as date, v as nav from nav", con)
        idx = pd.read_sql_query("select ticker, d as date, v as close from idx", con)
        bench = pd.read_sql_query("select benchmark, ticker, kind from bench_series", con)
    except Exception:
        nav, idx, bench = pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    con.close()

    d_out = funds_dir(root)
    holdings = pd.DataFrame(rows)
    schemes = schemes.merge(pd.DataFrame(scheme_rows), on="isin", how="outer",
                            suffixes=("", "_p"))
    schemes.to_parquet(d_out / "schemes.parquet", index=False)
    if not holdings.empty:
        holdings.to_parquet(d_out / "holdings.parquet", index=False)
    if not ter.empty:
        ter.to_parquet(d_out / "ter.parquet", index=False)
    if not mcap.empty:
        mcap.to_parquet(d_out / "stock_mcap.parquet", index=False)
    for name, frame in (("nav", nav), ("idx", idx), ("bench_map", bench)):
        if not frame.empty:
            frame.to_parquet(d_out / f"{name}.parquet", index=False)

    resolved = int(holdings["symbol"].notna().sum()) if not holdings.empty else 0
    return {
        "nav_rows": int(len(nav)),
        "nav_schemes": int(nav["code"].nunique()) if not nav.empty else 0,
        "index_rows": int(len(idx)),
        "schemes": int(len(schemes)),
        "portfolios": len(files),
        "holdings_rows": int(len(holdings)),
        "resolved_to_nse": resolved,
        "unresolved_rows": int(len(holdings)) - resolved,
        "top_unresolved": sorted(unresolved.items(), key=lambda x: -x[1])[:10],
        "source": str(src),
        "note": ("unresolved names are foreign listings, unlisted holdings, "
                 "debt instruments and cash - they stay in the store with a "
                 "null symbol rather than being dropped"),
    }


def _f(v):
    try:
        return float(str(v).replace(",", "").replace("₹", ""))
    except (TypeError, ValueError):
        return None


def _load(name: str, root=None) -> pd.DataFrame:
    p = funds_dir(root) / f"{name}.parquet"
    if not p.exists():
        raise DataError(f"fund store empty - POST /api/funds/import first ({name})")
    return pd.read_parquet(p)


def search_schemes(q: str, limit: int = 40, root=None) -> list[dict]:
    df = _load("schemes", root)
    m = df[df["name"].str.contains(re.escape(q), case=False, na=False)
           | df["amc"].str.contains(re.escape(q), case=False, na=False)]
    m = m.sort_values("aum", ascending=False).head(limit)
    return [{"isin": r.isin, "name": r.name, "amc": r.amc,
             "category": r.category, "aum_cr": r.aum,
             "benchmark": getattr(r, "benchmark", None)}
            for r in m.itertuples()]


def scheme_detail(isin: str, root=None) -> dict:
    df = _load("schemes", root)
    row = df[df["isin"] == isin]
    if row.empty:
        raise DataError(f"no scheme with ISIN {isin}")
    s = row.iloc[0].to_dict()
    out = {k: (None if pd.isna(v) else v) for k, v in s.items()}
    try:
        h = _load("holdings", root)
        mine = h[h["isin"] == isin].sort_values("weight_pct", ascending=False)
        out["holdings"] = [
            {k: (None if pd.isna(v) else v) for k, v in r.items()}
            for r in mine.head(120).to_dict("records")]
        secs = (mine.groupby("sector")["weight_pct"].sum()
                .sort_values(ascending=False).head(14))
        out["sectors"] = [{"sector": k, "weight_pct": round(float(v), 2)}
                          for k, v in secs.items()]
    except DataError:
        out["holdings"], out["sectors"] = [], []
    return out


def schemes_holding(symbol: str, root=None) -> dict:
    """Every scheme whose disclosed portfolio names this stock.

    This is the economic owner the SEBI filing cannot show: the AMC appears
    once in the shareholding pattern, but the position belongs to particular
    schemes with particular mandates."""
    sym = symbol.upper().replace(".NS", "")
    h = _load("holdings", root)
    mine = h[h["symbol"] == sym].sort_values("market_value_cr", ascending=False)
    total = float(mine["market_value_cr"].fillna(0).sum())
    by_amc: dict[str, float] = {}
    try:
        sch = _load("schemes", root)[["isin", "amc", "category"]]
        mine = mine.merge(sch, on="isin", how="left")
        by_amc = (mine.groupby("amc")["market_value_cr"].sum()
                  .sort_values(ascending=False).head(12).to_dict())
    except DataError:
        pass
    return {
        "symbol": sym,
        "n_schemes": int(len(mine)),
        "total_value_cr": round(total, 2),
        "as_of": (mine["as_of"].max() if len(mine) else None),
        "by_amc": [{"amc": k, "value_cr": round(float(v), 2)}
                   for k, v in by_amc.items()],
        "schemes": [{"isin": r.isin, "scheme": r.scheme_name,
                     "amc": getattr(r, "amc", None),
                     "category": getattr(r, "category", None),
                     "weight_pct": None if pd.isna(r.weight_pct) else r.weight_pct,
                     "value_cr": None if pd.isna(r.market_value_cr) else r.market_value_cr,
                     "change_1m_pct": None if pd.isna(r.change_1m_pct) else r.change_1m_pct}
                    for r in mine.head(150).itertuples()],
        "note": ("disclosed monthly portfolios; a scheme that has not filed "
                 "since its last refresh shows its last disclosure, dated"),
    }


def store_stats(root=None) -> dict:
    d = funds_dir(root)
    out: dict = {}
    for name in ("schemes", "holdings"):
        p = d / f"{name}.parquet"
        if p.exists():
            try:
                df = pd.read_parquet(p)
                out[name] = int(len(df))
                if name == "holdings":
                    out["symbols_resolved"] = int(df["symbol"].nunique())
                    out["as_of"] = str(df["as_of"].max())
            except Exception:
                out[name] = "unreadable"
        else:
            out[name] = 0
    return out


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------

_WINDOWS = {"1m": 21, "3m": 63, "6m": 126, "1y": 252, "3y": 756, "5y": 1260}


def _cagr(series: pd.Series, days: int) -> float | None:
    """Point-to-point return, annualised past a year. None when the fund is
    younger than the window - a 3-year number on a 2-year fund is a lie
    with a decimal point."""
    if len(series) <= days:
        return None
    last, first = float(series.iloc[-1]), float(series.iloc[-1 - days])
    if first <= 0:
        return None
    total = last / first - 1.0
    years = days / 252.0
    return float(((1 + total) ** (1 / years) - 1) * 100 if years > 1 else total * 100)


def scheme_performance(isin: str, root=None) -> dict:
    """Returns and risk from the fund's own NAV history, plus its benchmark.

    Everything here is computed from the stored series; nothing is taken on
    a factsheet's word."""
    import numpy as np

    schemes = _load("schemes", root)
    row = schemes[schemes["isin"] == isin]
    if row.empty:
        raise DataError(f"no scheme with ISIN {isin}")
    meta = row.iloc[0]
    code = meta.get("code")
    try:
        nav = _load("nav", root)
    except DataError:
        return {"isin": isin, "error": "no NAV series imported yet"}
    mine = nav[nav["code"] == code].sort_values("date")
    if len(mine) < 30:
        return {"isin": isin, "error": f"only {len(mine)} NAV points on file"}
    v = mine["nav"].astype(float).reset_index(drop=True)

    returns = {k: _cagr(v, d) for k, d in _WINDOWS.items()}
    r = np.log(v).diff().dropna()
    vol = float(r.tail(252).std() * np.sqrt(252) * 100) if len(r) > 60 else None
    roll_max = v.cummax()
    dd = float(((v / roll_max) - 1).min() * 100)

    out = {
        "isin": isin, "name": meta.get("name"), "code": code,
        "first": str(mine["date"].iloc[0]), "last": str(mine["date"].iloc[-1]),
        "points": int(len(mine)),
        "nav": float(v.iloc[-1]),
        "returns_pct": returns,
        "vol_1y_pct": vol,
        "max_drawdown_pct": round(dd, 2),
        "series": [{"date": str(d), "nav": float(x)}
                   for d, x in zip(mine["date"].iloc[::5], v.iloc[::5])][-500:],
        "note": ("returns are point-to-point, annualised beyond one year; a "
                 "window longer than the fund's life reports nothing rather "
                 "than a shortened one"),
    }

    # ---- benchmark, when the mapping knows one --------------------------
    bm = meta.get("benchmark")
    out["benchmark"] = bm
    try:
        bmap = _load("bench_map", root)
        idx = _load("idx", root)
        tick = bmap[bmap["benchmark"] == bm]
        if not tick.empty:
            t = tick.iloc[0]["ticker"]
            ser = idx[idx["ticker"] == t].sort_values("date")
            if len(ser) > 30:
                bv = ser["close"].astype(float).reset_index(drop=True)
                out["benchmark_ticker"] = t
                out["benchmark_returns_pct"] = {k: _cagr(bv, d)
                                                for k, d in _WINDOWS.items()}
                out["excess_pct"] = {
                    k: (None if out["returns_pct"].get(k) is None
                        or out["benchmark_returns_pct"].get(k) is None
                        else round(out["returns_pct"][k] - out["benchmark_returns_pct"][k], 2))
                    for k in _WINDOWS}
    except DataError:
        pass
    return out


def category_table(category: str, window: str = "1y", root=None) -> dict:
    """Every scheme in a category ranked on one window - the quartile view."""
    schemes = _load("schemes", root)
    nav = _load("nav", root)
    days = _WINDOWS.get(window, 252)
    pool = schemes[schemes["category"].astype(str).str.contains(
        re.escape(category), case=False, na=False)]
    rows = []
    for r in pool.itertuples():
        mine = nav[nav["code"] == r.code].sort_values("date")
        if len(mine) <= days:
            continue
        ret = _cagr(mine["nav"].astype(float).reset_index(drop=True), days)
        if ret is None:
            continue
        rows.append({"isin": r.isin, "name": r.name, "amc": r.amc,
                     "aum_cr": None if pd.isna(r.aum) else float(r.aum),
                     "return_pct": round(ret, 2)})
    rows.sort(key=lambda x: -x["return_pct"])
    n = len(rows)
    for i, x in enumerate(rows):
        x["rank"] = i + 1
        x["quartile"] = min(4, int(i / max(n, 1) * 4) + 1)
    return {"category": category, "window": window, "n": n, "rows": rows,
            "note": ("ranked on the stored NAV series; schemes younger than "
                     "the window are absent, not zero-filled")}
