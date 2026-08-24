"""Build the knowledge graph from everything the terminal already stores.

Each store answers one question well and none of them talk to each other:
the ownership registry knows who holds what, the fund store knows which
scheme holds what, the constituent lists know sectors, MSCI knows index
membership, the supply maps know inputs and customers. Ingest turns all of
that into one graph so a question can cross sources - "which schemes hold
the companies this family controls" is a two-hop query afterwards and an
afternoon of scripting before.

Every edge carries the source it came from. Ingest imports; it never
derives a relationship that no store asserted.
"""

from __future__ import annotations

import json

import pandas as pd

from shunkan.store.graph import (canonical_name, graph, node_id,
                                 split_beneficial_owners)


def _p(name: str):
    from shunkan.store.store import STORE_DIR

    return STORE_DIR / name


def rebuild(verbose=None) -> dict:
    """Rebuild the graph from the parquet stores. Idempotent."""
    g = graph()
    out: dict = {}
    say = verbose or (lambda *_: None)

    # ---- companies from the constituent lists ---------------------------
    try:
        from shunkan.data.constituents import universe

        cons = universe(("NIFTY500",))
        for c in cons:
            nid = g.put_node("company", c.symbol, c.name,
                             {"industry": c.industry, "indices": list(c.indices)})
            g.put_alias(c.name, nid, "NSE constituents")
            g.put_alias(c.symbol, nid, "NSE constituents")
            if c.industry:
                sid = g.put_node("sector", c.industry, c.industry)
                g.put_edges([{"src": nid, "dst": sid, "rel": "in_sector",
                              "source": "NSE constituent taxonomy"}])
        out["companies"] = len(cons)
        g.commit()
        say(f"  companies: {len(cons)}")
    except Exception as exc:
        out["companies_error"] = str(exc)[:120]

    # ---- ownership registry: holders -> companies -----------------------
    path = _p("ownership") / "holders.parquet"
    if path.exists():
        df = pd.read_parquet(path)
        edges, n_h = [], 0
        for r in df.itertuples():
            if not isinstance(r.symbol, str) or len(r.symbol.strip()) < 2:
                continue
            cid = g.resolve(r.symbol, "company") or g.put_node("company", r.symbol, r.symbol)
            # One institution, one node, whatever the filing called it.
            canon = canonical_name(str(r.holder))[:80]
            hid = node_id("holder", canon)
            g.put_node("holder", canon, canon,
                       {"kind": getattr(r, "kind", None), "bucket": r.bucket})
            g.put_alias(str(r.holder), hid, "NSE shareholding XBRL")
            g.put_alias(canon, hid, "canonical map")
            n_h += 1
            edges.append({"src": hid, "dst": cid, "rel": "holds",
                          "weight": None if pd.isna(r.pct) else float(r.pct),
                          "unit": "pct", "as_of": r.as_of,
                          "source": "NSE shareholding pattern XBRL",
                          "meta": {"bucket": r.bucket, "shares": None if pd.isna(r.shares) else int(r.shares)}})
            bo = getattr(r, "beneficial_owner", "")
            if isinstance(bo, str) and bo.strip():
                # One declaration can name a whole family; each becomes a
                # person, so "which companies does X control" is answerable
                # per human rather than per boilerplate string.
                for person in split_beneficial_owners(bo):
                    pid = g.put_node("person", person[:80], person)
                    g.put_alias(person, pid, "SBO declaration")
                    edges.append({"src": hid, "dst": pid, "rel": "beneficial_owner",
                                  "as_of": r.as_of,
                                  "source": "SBO declaration, Companies Act s.90"})
        g.put_edges(edges)
        g.commit()
        out["ownership_edges"] = len(edges)
        say(f"  ownership: {len(edges)} edges, {n_h} holder rows")

    # ---- fund store: schemes -> companies, schemes -> amc ---------------
    hpath = _p("funds") / "holdings.parquet"
    spath = _p("funds") / "schemes.parquet"
    if hpath.exists():
        h = pd.read_parquet(hpath)
        schemes = pd.read_parquet(spath) if spath.exists() else pd.DataFrame()
        amc_by_isin = (dict(zip(schemes["isin"], schemes["amc"]))
                       if not schemes.empty else {})
        cat_by_isin = (dict(zip(schemes["isin"], schemes["category"]))
                       if not schemes.empty else {})
        seen_scheme: set[str] = set()
        edges = []
        for r in h.itertuples():
            if not isinstance(r.symbol, str) or len(r.symbol.strip()) < 2:
                continue          # cash rows: real, but not a company edge
            cid = g.resolve(r.symbol, "company") or g.put_node("company", r.symbol, r.symbol)
            sid = node_id("scheme", r.isin)
            if r.isin not in seen_scheme:
                g.put_node("scheme", r.isin, str(r.scheme_name),
                           {"amc": amc_by_isin.get(r.isin),
                            "category": cat_by_isin.get(r.isin)})
                g.put_alias(str(r.scheme_name), sid, "mfresearch")
                seen_scheme.add(r.isin)
                amc = amc_by_isin.get(r.isin)
                if isinstance(amc, str) and amc:
                    aid = g.put_node("amc", amc, amc)
                    edges.append({"src": sid, "dst": aid, "rel": "managed_by",
                                  "source": "mfresearch scheme master"})
            edges.append({"src": sid, "dst": cid, "rel": "scheme_holds",
                          "weight": None if pd.isna(r.weight_pct) else float(r.weight_pct),
                          "unit": "pct_of_scheme", "as_of": str(r.as_of),
                          "source": "AMC disclosed portfolio (mfresearch)",
                          "meta": {"value_cr": None if pd.isna(r.market_value_cr)
                                   else float(r.market_value_cr)}})
        g.put_edges(edges)
        g.commit()
        out["fund_edges"] = len(edges)
        out["schemes"] = len(seen_scheme)
        say(f"  funds: {len(edges)} edges, {len(seen_scheme)} schemes")

    # ---- MSCI: index membership and pending calls -----------------------
    mpath = _p("msci") / "predictions.parquet"
    if mpath.exists():
        df = pd.read_parquet(mpath)
        edges = []
        for r in df.itertuples():
            call = (r.call or "").strip()
            if not call or call.lower() == "hold":
                continue
            cid = g.resolve(r.symbol, "company") or g.put_node("company", r.symbol, r.symbol)
            iid = g.put_node("index", "MSCI", "MSCI India")
            edges.append({"src": cid, "dst": iid, "rel": "index_call",
                          "weight": None if pd.isna(r.p_in_index) else float(r.p_in_index),
                          "unit": "probability",
                          "source": "msci rule engine prediction",
                          "meta": {"call": call, "current": r.current_index,
                                   "reason": (r.reason or "")[:200]}})
        g.put_edges(edges)
        g.commit()
        out["msci_edges"] = len(edges)
        say(f"  msci: {len(edges)} calls")

    out["stats"] = g.stats()
    return out


def cache_company(symbol: str, payload: dict, ttl_s: float = 6 * 3600) -> None:
    graph().cache_put(f"company:{symbol.upper()}", payload, "company", ttl_s)


def cached_company(symbol: str, max_age_s: float | None = None):
    return graph().cache_get(f"company:{symbol.upper()}", max_age_s)
