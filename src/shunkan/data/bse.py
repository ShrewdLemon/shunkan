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
