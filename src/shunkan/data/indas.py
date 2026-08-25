"""Ind AS quarterly filings: headline numbers and SEGMENT reporting.

Yahoo serves annual statements. The exchange holds every quarter, filed as
XBRL under LODR Reg 33, and inside each one is the thing no free summary
carries: Ind AS 108 segment reporting - revenue and profit split by the
businesses the company actually runs.

A WARNING THE FILINGS EARNED. The segment tables are tagged by COLUMN, not
by period: "OneReportableSegmentRevenue01D" and "FourReportableSegment
Revenue01D" both declare the same start and end date while carrying
different numbers, because they are different columns of the same printed
table (the quarter, the year-to-date, the prior year). Guessing which
column is the quarter would silently mislabel every number downstream, so
this module does not guess: it totals each column, compares against the
headline revenue in the same filing, and reports which column matched and
by how much. When nothing matches, it says so and returns the columns
unlabelled rather than picking one.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict

from shunkan.data.provider import DataError

_NS = {"x": "http://www.xbrl.org/2003/instance"}
_H = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36"),
      "Referer": "https://www.nseindia.com/"}


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def fetch_indas(url: str) -> str:
    import httpx

    try:
        r = httpx.get(url, headers=_H, timeout=60.0, follow_redirects=True)
    except Exception as exc:
        raise DataError(f"Ind AS filing unreachable: {exc}") from exc
    if r.status_code != 200 or len(r.text) < 4000:
        raise DataError(f"Ind AS filing not served (HTTP {r.status_code})")
    return r.text


def parse_indas(xml: str) -> dict:
    """Headline P&L plus segment columns, with the column check attached."""
    root = ET.fromstring(xml.encode())

    periods: dict[str, tuple] = {}
    for c in root.findall("x:context", _NS):
        per = c.find(".//x:period", _NS)
        if per is None:
            continue
        periods[c.get("id")] = (
            per.findtext("x:startDate", default="", namespaces=_NS)
            or per.findtext("x:instant", default="", namespaces=_NS),
            per.findtext("x:endDate", default="", namespaces=_NS),
        )

    facts: dict[str, dict] = defaultdict(dict)
    for el in root.iter():
        ref = el.get("contextRef")
        if ref and el.text and el.text.strip():
            facts[ref][el.tag.split("}")[-1]] = el.text.strip()

    # ---- headline lines. The context id IS the column ("OneD", "FourD"),
    # which is what links a headline to the segment rows of the same column.
    HEAD = ("RevenueFromOperations", "OtherIncome", "TotalIncome",
            "TotalExpenses", "ProfitBeforeTax", "InterSegmentRevenue",
            "ProfitLossForPeriodFromContinuingOperations",
            "ProfitLossForPeriod", "EarningsPerShareBasic")
    by_column: dict[str, dict] = defaultdict(dict)
    for ref, f in facts.items():
        col = re.match(r"^([A-Za-z]+?)[DI]$", ref)
        if not col:
            continue
        for tag in HEAD:
            if tag in f:
                v = _num(f[tag])
                if v is not None:
                    by_column[col.group(1)][tag] = v
        if ref in periods:
            by_column[col.group(1)]["period"] = periods[ref]

    # ---- segment rows, keyed by the same column prefix -------------------
    columns: dict[str, dict] = defaultdict(dict)
    for ref, f in facts.items():
        name = f.get("DescriptionOfReportableSegment")
        if not name:
            continue
        m = re.match(r"([A-Za-z]+?)ReportableSegment(\w+?)\d+[DI]?$", ref)
        if not m:
            continue
        col = m.group(1)
        entry = columns[col].setdefault(name, {"segment": name})
        for tag, key in (("SegmentRevenue", "revenue"),
                         ("SegmentProfitLossBeforeTaxAndFinanceCosts", "profit"),
                         ("SegmentAssets", "assets"),
                         ("SegmentLiabilities", "liabilities")):
            if tag in f:
                entry[key] = _num(f[tag])

    # ---- which column is the QUARTER --------------------------------------
    # Not guessed from the prefix and not inferred from the period (segment
    # contexts declare identical dates for every column). The quarter is the
    # column with the SMALLEST headline revenue, because a year-to-date
    # column contains the quarter and cannot be smaller than it.
    revs = {c: v.get("RevenueFromOperations") for c, v in by_column.items()
            if v.get("RevenueFromOperations")}
    quarter_col = min(revs, key=revs.get) if revs else None
    ytd_col = max(revs, key=revs.get) if len(revs) > 1 else None

    def rows_for(col):
        return sorted(columns.get(col, {}).values(),
                      key=lambda s: -(s.get("revenue") or 0)) if col else []

    # The residual is REPORTED, not required to be zero: segment revenue is
    # gross of eliminations a filing does not always tag, and pretending
    # otherwise would either hide a real gap or reject a good filing.
    recon = None
    if quarter_col:
        seg_total = sum((s.get("revenue") or 0) for s in rows_for(quarter_col))
        head = by_column[quarter_col].get("RevenueFromOperations")
        inter = by_column[quarter_col].get("InterSegmentRevenue")
        if seg_total and head:
            recon = {"segment_total": seg_total, "headline": head,
                     "inter_segment": inter,
                     "residual": seg_total - (inter or 0) - head,
                     "residual_pct": round((seg_total - (inter or 0) - head) / head * 100, 2)}

    return {
        "columns": {c: v for c, v in by_column.items()},
        "quarter_column": quarter_col,
        "ytd_column": ytd_col,
        "segments": rows_for(quarter_col),
        "segments_ytd": rows_for(ytd_col),
        "headline": by_column.get(quarter_col, {}),
        "reconciliation": recon,
        "note": ("segment tables are tagged by table COLUMN, not by period - "
                 "every column declares the same dates. The quarter is the "
                 "column with the smallest headline revenue, since a "
                 "year-to-date column contains it. Any residual between "
                 "segment revenue and headline revenue is reported, not "
                 "forced to zero."),
    }


def segments_for(symbol: str, max_filings: int = 4) -> dict:
    """The newest quarterly filing that actually parses, with its segments."""
    from shunkan.data.filings import quarterly_results

    tried = []
    filings = quarterly_results(symbol, max_filings)
    # Consolidated first: the standalone filing of a holding company shows
    # almost nothing for segments run through subsidiaries (Reliance's
    # standalone Retail revenue is Rs 19 crore, the group's is not).
    filings.sort(key=lambda q: 0 if "non" not in str(q.get("basis", "")).lower() else 1)
    for q in filings:
        url = q.get("xbrl")
        if not url:
            continue
        try:
            parsed = parse_indas(fetch_indas(url))
        except DataError as exc:
            tried.append({"period": q.get("to"), "error": str(exc)[:90]})
            continue
        if not parsed.get("segments"):
            tried.append({"period": q.get("to"), "error": "filing carries no segment table"})
            continue
        parsed.update({"symbol": symbol.upper(), "period_to": q.get("to"),
                       "basis": q.get("basis"), "filed": q.get("filed"),
                       "source": url, "skipped": tried})
        return parsed
    raise DataError(f"no parseable Ind AS filing with segments for {symbol} "
                    f"(tried {len(tried)})")
