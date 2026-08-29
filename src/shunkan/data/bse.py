"""BSE as a second exchange source, and the related-party graph NSE has never had.

WHY THIS EXISTS. The whole company database was built from NSE filings, and
NSE was treated as the only source until someone asked why. Two things came
back from actually checking:

  COVERAGE. NSE's corporate-filing API returns an EMPTY payload for some
  listed names - ABBOTINDIA and MCX give zero annual reports AND zero board
  meetings, while VOLTAS on the same client returns 17 rows. Both are on BSE
  with 29 and 15 annual reports respectively, FY2026 included. BSE also lists
  ~4,979 active equity scrips against NSE's ~2,000, so past NIFTY 500 it is
  the broader source rather than the fallback.

  THE RPT FEED. BSE publishes half-yearly related-party transactions as
  structured XBRL: ~2,000 rows per large company, each naming the entity, the
  counterparty, the relationship ("Fellow Subsidiary", "Subsidiary") and the
  transaction type ("Purchase of goods or services", "Sale of goods or
  services") with values. That is a named counterparty graph with direction,
  free and machine-readable - the thing this project has otherwise been
  reconstructing sentence by sentence out of PDF prose.

THE ACCESS GATE. api.bseindia.com 301-redirects to a members page unless the
request carries Referer: https://www.bseindia.com/. It ALSO needs an Accept
header or it answers 406. Neither is documented; both were found by trying.

THE RPT FEED IS FROZEN AND DEPRECATED. Data exists for six half-yearly
periods, Mar 2022 to Sep 2024, and every scripcode checked stops at the same
place. SEBI's Integrated Filing regime (BSE circular, 2 Apr 2025) folded RPT
into consolidated filings, and the replacement endpoints - IntegratedFileData
and IntegratedFinanceData - carry compliance CHECKBOXES rather than
transaction detail. RPT XBRL is still mandatory and still enforced; BSE simply
stopped surfacing the rows. So this corpus should be harvested and kept
locally rather than fetched on demand, because the endpoint serving it is one
nobody is maintaining.
"""

from __future__ import annotations

import json
import time

from shunkan.data.provider import DataError

API = "https://api.bseindia.com/BseIndiaAPI/api"

# Referer is the gate; Accept stops a 406. A User-Agent is not required but is
# sent anyway so the traffic is identifiable.
_H = {
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}


def _get(path: str, timeout: float = 90.0) -> dict | list:
    import httpx

    try:
        r = httpx.get(f"{API}/{path}", headers=_H, timeout=timeout,
                      follow_redirects=True)
    except Exception as exc:
        raise DataError(f"BSE unreachable: {exc}") from exc
    if r.status_code == 301:
        raise DataError("BSE redirected to the members page - the Referer "
                        "header is missing")
    if r.status_code == 406:
        raise DataError("BSE answered 406 - the Accept header is missing")
    if r.status_code != 200:
        raise DataError(f"BSE HTTP {r.status_code}")
    try:
        return r.json()
    except Exception as exc:
        raise DataError(f"BSE returned non-JSON: {r.text[:120]}") from exc


def _cache_dir():
    from shunkan.store.store import STORE_DIR

    d = STORE_DIR / "bse"
    d.mkdir(parents=True, exist_ok=True)
    return d


def scrip_master(refresh: bool = False) -> list[dict]:
    """All active equity scrips. ~4,979 rows, cached for a week."""
    path = _cache_dir() / "scrip_master.json"
    if path.exists() and not refresh:
        age = time.time() - path.stat().st_mtime
        if age < 7 * 86400:
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
    rows = _get("ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status=Active")
    if not isinstance(rows, list) or not rows:
        raise DataError("BSE scrip master came back empty")
    path.write_text(json.dumps(rows))
    return rows


def scrip_code(symbol: str) -> int:
    """NSE symbol -> BSE scrip code.

    Joins on BSE's `scrip_id`, which is usually the same ticker NSE uses. ISIN
    would be the stronger key but the NSE constituent CSVs do not carry one,
    so the fallback is a normalised name match rather than a guess.
    """
    sym = symbol.upper().replace(".NS", "").strip()
    rows = scrip_master()
    for r in rows:
        if str(r.get("scrip_id", "")).upper().strip() == sym:
            return int(r["SCRIP_CD"])
    # fall back to the issuer name, stripped of legal form
    import re

    def norm(s):
        s = re.sub(r"[^A-Z0-9 ]", " ", str(s).upper())
        s = re.sub(r"\b(LIMITED|LTD|PRIVATE|PVT|THE|AND|OF|INDIA)\b", " ", s)
        return re.sub(r"\s+", "", s)

    target = norm(sym)
    for r in rows:
        if norm(r.get("Scrip_Name")) == target or norm(r.get("Issuer_Name")) == target:
            return int(r["SCRIP_CD"])
    raise DataError(f"no BSE scrip code for {sym}")


def annual_reports(symbol: str) -> list[dict]:
    """Annual reports from BSE, newest first.

    Shaped like filings.annual_reports so a caller can fall back to this
    without special-casing: {url, to_year, size}.
    """
    code = scrip_code(symbol)
    rows = _get(f"AnnualReport_New/w?scripcode={code}")
    table = rows.get("Table", []) if isinstance(rows, dict) else []
    out = []
    for r in table:
        url = r.get("PDFDownload")
        if not url:
            continue
        out.append({"url": url, "to_year": str(r.get("Year") or ""),
                    "size": "", "source": "BSE", "scrip_code": code})
    out.sort(key=lambda r: r["to_year"], reverse=True)
    if not out:
        raise DataError(f"BSE lists no annual reports for {symbol}")
    return out


# ------------------------------------------------------- related-party graph

def rpt_periods(symbol_or_code) -> list[dict]:
    """The half-yearly periods BSE will serve RPT rows for.

    Returns newest first with the `qtrid` each detail call needs. That
    parameter is the whole trick: `period=` and `Fld_ResultId=` both return an
    empty object, which is why this endpoint looked broken.
    """
    code = (symbol_or_code if isinstance(symbol_or_code, int)
            else scrip_code(symbol_or_code))
    j = _get(f"XbrlRelatedPartyTrans/w?scripcode={code}")
    table = j.get("Table", []) if isinstance(j, dict) else []
    return [{"qtrid": r.get("qtrid"), "qtr": r.get("qtr"), "yr": r.get("yr"),
             "filed": r.get("filing_date_time"), "xbrl": r.get("xbrlurl")}
            for r in table if r.get("qtrid") is not None]


# Value columns are stated in the units named by the header, not in rupees.
_ROUNDING = {"MILLIONS": 1e6, "LAKHS": 1e5, "CRORES": 1e7,
             "THOUSANDS": 1e3, "ACTUAL": 1.0, "UNITS": 1.0}


def rpt_rows(symbol_or_code, qtrid) -> dict:
    """Transaction-level related-party rows for one period.

    Every value is scaled into rupees using the filing's own level of
    rounding, which VARIES BY FILER - MILLIONS for one company, LAKHS for the
    next. Leaving the raw number in place would silently mix magnitudes across
    the corpus, which is the kind of error that looks like data.
    """
    code = (symbol_or_code if isinstance(symbol_or_code, int)
            else scrip_code(symbol_or_code))
    j = _get(f"XbrlRPTDetailsNewFormat/w?scripcode={code}&qtrid={qtrid}")
    if not isinstance(j, dict):
        raise DataError("unexpected RPT payload")
    hdr = (j.get("Table") or [{}])[0]
    unit = str(hdr.get("Levelofrounding") or "").upper().strip()
    mult = _ROUNDING.get(unit)
    rows = []
    for r in (j.get("Table2") or []):
        cp = (r.get("Fld_NameOfCounterParty") or "").strip()
        if not cp:
            continue
        val = r.get("Fld_AmountOfRPTDuringPeriod")
        rows.append({
            "entity": (r.get("Fld_NameOfListedEntity") or "").strip(),
            "counterparty": cp,
            "relationship": (r.get("Fld_RelationshipOfTheCounterparty") or "").strip(),
            "type": (r.get("Fld_TypeOfRPT") or "").strip(),
            # None rather than a wrong number when the unit is unrecognised:
            # an unscaled figure is not a smaller error than a missing one.
            "amount_inr": (float(val) * mult) if (mult and val is not None) else None,
            "raw_amount": val,
            "unit": unit or None,
        })
    return {"scrip_code": code, "qtrid": qtrid, "unit": unit or None,
            "scaled": bool(mult), "rows": rows}


def harvest_rpt(symbol_or_code, *, sleep_s: float = 0.6) -> dict:
    """Every available period for one company, stored locally.

    Stored because the endpoint is deprecated: SEBI's Integrated Filing regime
    folded RPT into consolidated filings and BSE stopped surfacing transaction
    detail after Sep 2024. What is served today is a frozen corpus on an
    unmaintained path, so it is worth having on disk rather than assuming it
    will answer tomorrow.
    """
    code = (symbol_or_code if isinstance(symbol_or_code, int)
            else scrip_code(symbol_or_code))
    periods = rpt_periods(code)
    out, total = [], 0
    for p in periods:
        try:
            d = rpt_rows(code, p["qtrid"])
        except DataError:
            continue
        if d["rows"]:
            out.append({**p, "n": len(d["rows"]), "unit": d["unit"],
                        "rows": d["rows"]})
            total += len(d["rows"])
        time.sleep(sleep_s)
    path = _cache_dir() / f"rpt_{code}.json"
    path.write_text(json.dumps({"scrip_code": code, "periods": out}, indent=1))
    return {"scrip_code": code, "periods": len(out), "rows": total,
            "path": str(path)}


# --------------------------------------------------------- graph projection

# The filed relationship string is free text and filers disagree on all of
# case, plural and specificity: "Fellow Subsidiary" / "Fellow subsidiary",
# "Subsidiary" / "Subsidiaries" / "Wholly Owned Subsidiary", and company-
# specific strings like "Subsidiaries of TCS" or "Wholly owned Subsidiary of
# APSEZ". Left raw they would shatter one relation into a dozen edge types.
# Ordered longest-intent-first because "subsidiary of ultimate parent" must
# not be eaten by the plain "subsidiary" rule.
# THE HEAD NOUN GOVERNS, NOT WHICHEVER WORD APPEARS FIRST IN THE RULE LIST.
#
# Filers write headed phrases: "Director of Subsidiary Company" names a
# PERSON, and "subsidiary" is a qualifier saying which board they sit on. A
# flat substring scan read the qualifier as the head and classified 528 HDFC
# Bank rows as subsidiaries - including named individuals and their relatives.
# The page would then have asserted, with a source attached, that HDFC Bank
# has 528 subsidiaries. It has roughly ten.
#
# So person-role terms are tested FIRST. In this corpus "subsidiary",
# "associate" and "fellow subsidiary" appear in person phrases only ever as
# the object of "of", never as the head, which makes the precedence safe:
# there is no filed phrase where a person term appears as a qualifier on a
# structural head.
_PERSON_ENTITY = (
    "interested entity", "entity in which", "enterprise over which",
    "enterprise in which", "firm in which", "concern in which",
)
_RELATIVE = ("relative", "spouse", "son of", "daughter of", "huf")
_PERSON = ("kmp", "key management", "key managerial", "director", "manager",
           "whole-time", "whole time", "chairman", "officer")

_REL_RULES = (
    # structural, longest intent first
    ("ultimate parent", "subsidiary_of_ultimate_parent"),
    ("holding", "holding_company_of"),
    ("fellow", "fellow_subsidiary_of"),
    ("joint venture", "joint_venture_with"),
    ("joint control", "significant_influence_over"),
    ("significant influence", "significant_influence_over"),
    ("associate", "associate_of"),
    ("promoter", "promoter_group_of"),
    ("wholly owned subsidiary", "wholly_owned_subsidiary_of"),
    ("wholly-owned subsidiary", "wholly_owned_subsidiary_of"),
    ("subsidiar", "subsidiary_of"),
)


def normalise_relationship(raw: str) -> str:
    """Map a filer's own relationship text onto one graph relation.

    Person-headed phrases resolve to a person relation even when they name a
    structural entity as their qualifier, because that entity is not the party
    being described - the individual is.
    """
    low = (raw or "").lower()
    if not low.strip():
        return "related_party_of"

    # An entity someone is INTERESTED IN is neither that person nor a
    # subsidiary of the filer. It gets its own relation rather than being
    # flattened into key management, which would call a company a person.
    if any(t in low for t in _PERSON_ENTITY):
        return "kmp_interested_entity_of"
    if any(t in low for t in _RELATIVE):
        # A promoter's relative is promoter group as a matter of law; a
        # director's relative is not.
        return "promoter_group_of" if "promoter" in low else "relative_of_kmp"
    if any(t in low for t in _PERSON):
        return "key_management_of"

    for needle, rel in _REL_RULES:
        if needle in low:
            return rel
    return "related_party_of"


# ONLY these carry commercial direction. "Any other transaction" is 52% of all
# rows and says nothing about who supplied whom - turning it into a trade edge
# would manufacture a supply relationship out of a disclosure catch-all, which
# is the exact failure this project exists to avoid. It still proves the
# parties are related, so it lands as a relationship edge and nothing more.
_TRADE = {
    "sale of goods or services": ("rpt_sells_to", 1),
    "sale of goods": ("rpt_sells_to", 1),
    "sale of fixed assets": ("rpt_sells_to", 1),
    "purchase of goods or services": ("rpt_buys_from", 1),
    "purchase of goods": ("rpt_buys_from", 1),
    "purchase of fixed assets": ("rpt_buys_from", 1),
}


def _node_for(g, name, src, cache):
    """Memoised resolve-or-create for one ingest run."""
    from shunkan.store.graph import normalise

    key = normalise(name)
    if key in cache:
        return cache[key]
    nid = g.resolve(name) or g.put_node("company", key or name, name)
    g.put_alias(name, nid, source=src)
    cache[key] = nid
    return nid


def ingest_rpt(symbol_or_code, *, symbol: str | None = None) -> dict:
    """Push one company's harvested RPT into the knowledge graph.

    ENTITY RESOLUTION IS THE POINT. "Reliance Retail Limited" arriving from
    the RPT feed must land on the SAME node the annual-report extraction
    created for it, or the graph shows one company twice and looks
    authoritative doing it. Resolution goes through the alias table and
    normalise(), which already strip legal form, so "Reliance Retail Limited"
    and "Reliance Retail Ltd" converge.

    Values are already in rupees - rpt_rows applies the filing's own level of
    rounding - so the weight on a trade edge is comparable across companies.
    Rows whose unit could not be recognised carry no weight rather than a
    wrong one.
    """
    from shunkan.store.graph import GraphStore, normalise

    code = (symbol_or_code if isinstance(symbol_or_code, int)
            else scrip_code(symbol_or_code))
    path = _cache_dir() / f"rpt_{code}.json"
    if not path.exists():
        raise DataError(f"no harvested RPT for scrip {code} - run harvest_rpt first")
    data = json.loads(path.read_text())

    g = GraphStore()
    sym = (symbol or (symbol_or_code if isinstance(symbol_or_code, str) else "")).upper()
    src = f"BSE RPT XBRL scrip {code}"
    edges, seen_rel, _cache = [], set(), {}

    # THE SPINE. The `entity` column names whichever GROUP COMPANY transacted,
    # not the filer - Reliance's own filing is mostly rows where the entity is
    # "Reliance Retail Limited". Attributing edges only to that column left
    # RELIANCE unconnected to its own group: the subsidiaries were richly
    # linked to each other and orphaned from the ticker anyone would search
    # for. So the filer is linked to every distinct entity appearing in its
    # filing, which is what makes RELIANCE -> Reliance Retail -> Neolync
    # walkable from the symbol.
    filer = None
    if sym:
        filer = g.resolve(sym) or g.put_node("company", sym, sym)
        g.put_alias(sym, filer, source=src)
    filed_entities = set()

    for per in data.get("periods", []):
        as_of = str(per.get("qtr") or "")
        for r in per.get("rows", []):
            ent, cp = r.get("entity") or sym, r["counterparty"]
            if not cp:
                continue
            # Resolve once per distinct name. The same counterparty recurs
            # across six periods and thousands of rows, and resolve() is a
            # UNION query - re-running it per row turned a 20-second ingest
            # into one that had not finished in ten minutes.
            e_id, c_id = _node_for(g, ent, src, _cache), _node_for(g, cp, src, _cache)

            if filer and e_id != filer and e_id not in filed_entities:
                filed_entities.add(e_id)
                edges.append({"src": filer, "dst": e_id, "rel": "group_entity_of",
                              "as_of": "", "source": src,
                              "meta": {"note": "named as a transacting entity in "
                                               "this filer's RPT disclosure"}})

            rel = normalise_relationship(r.get("relationship"))
            key = (e_id, c_id, rel)
            if key not in seen_rel:          # structural edge, once, not per period
                seen_rel.add(key)
                edges.append({"src": e_id, "dst": c_id, "rel": rel, "as_of": "",
                              "source": src,
                              "meta": {"stated": r.get("relationship")}})

            trade = _TRADE.get((r.get("type") or "").strip().lower())
            if trade and r.get("amount_inr"):
                edges.append({"src": e_id, "dst": c_id, "rel": trade[0],
                              "weight": r["amount_inr"], "unit": "INR",
                              "as_of": as_of, "source": src,
                              "meta": {"type": r.get("type"),
                                       "relationship": r.get("relationship")}})
    if edges:
        g.put_edges(edges)
    g.commit()
    return {"scrip_code": code, "symbol": sym, "edges": len(edges),
            "relationships": len(seen_rel), "group_entities": len(filed_entities)}


def link_legal_names(symbols=None) -> dict:
    """Alias each company's LEGAL NAME onto its ticker node.

    The graph keys companies by ticker - the node is RELIANCE - while every
    filing, every RPT row and every counterparty list writes "Reliance
    Industries Limited". Without a bridge those are two entities, and
    resolve("Reliance Industries Limited") found an `input` node created
    because some other filer named Reliance as a supplier. The trail then
    started from the wrong place and returned nothing, with no error.

    BSE's scrip master is the clean source for the mapping: it carries
    SCRIP_CD, scrip_id (the ticker) and Issuer_Name/Scrip_Name for ~4,979
    active scrips, so the join needs no guessing.
    """
    from shunkan.store.graph import GraphStore

    g = GraphStore()
    rows = scrip_master()
    by_ticker = {}
    for r in rows:
        t = str(r.get("scrip_id", "")).upper().strip()
        if t:
            by_ticker[t] = r
    if symbols is None:
        from shunkan.data.constituents import fetch_constituents

        symbols = [c.symbol for c in fetch_constituents("NIFTY500")]

    linked, missed = 0, []
    for sym in symbols:
        r = by_ticker.get(sym.upper())
        if not r:
            missed.append(sym)
            continue
        nid = g.resolve(sym, kind="company")
        if not nid:
            missed.append(sym)
            continue
        for field in ("Issuer_Name", "Scrip_Name"):
            name = str(r.get(field) or "").strip()
            if name:
                g.put_alias(name, nid, source="BSE scrip master")
                linked += 1
    g.commit()
    return {"aliased": linked, "unmatched": missed}
