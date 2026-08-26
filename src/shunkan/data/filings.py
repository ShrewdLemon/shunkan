"""NSE corporate filings: ownership registry and annual reports.

This is the layer a Bloomberg terminal charges for, assembled from what
Indian regulation actually forces into the open:

SHAREHOLDING PATTERN (SEBI LODR Reg 31, quarterly, XBRL). The exchange
publishes it as structured XBRL, not a PDF, which means the promoter
group is nameable entity by entity - individuals, family trusts, holding
companies - alongside every public holder above 1%. This is the holder
registry the company view previously refused for want of a source. The
refusal was right at the time and is retired now that the source is real.

ANNUAL REPORTS (LODR Reg 34). PDFs on nsearchives, one per year back a
decade or more. The supply-chain story lives in here - related party
transactions name the corporate family with rupee values attached,
segment notes split revenue by business and geography, and the
management discussion names raw materials and end markets.

What this module does NOT do: guess. A section that cannot be located in
the document is reported missing with the page count that was searched,
never summarised from the model's own knowledge of the company.
"""

from __future__ import annotations

import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from collections import defaultdict
from dataclasses import dataclass, field

from shunkan.data.provider import DataError

_H = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
_BASE = "https://www.nseindia.com"


def _client():
    """A cookie-warmed NSE session. The archive rejects cold clients."""
    import httpx

    c = httpx.Client(headers=_H, timeout=30.0, follow_redirects=True)
    try:
        c.get(_BASE)
    except Exception as exc:
        c.close()
        raise DataError(f"NSE unreachable: {exc}") from exc
    return c


# ---------------------------------------------------------------------------
# Shareholding pattern
# ---------------------------------------------------------------------------

# The XBRL pairs every holder across two contexts: D_<Group>_Context<N> carries
# the NAME, <Group>_Context<N> the numbers. Joining on the stripped prefix is
# the whole trick.
_PLEDGE_TAGS = (
    "SharesPledgedOrOtherwiseEncumberedAsAPercentageOfTotalShares",
    "NumberOfSharesPledgedOrOtherwiseEncumbered",
)
_PCT = "ShareholdingAsAPercentageOfTotalNumberOfShares"
_PCT_SCRR = ("ShareholdingAsAPercentageOfTotalNumberOfSharesCalculatedAsPer"
             "SCRR1957AsAPercentageOfAPlusBPlusC2")
_VOTING = "PercentageOfTotalVotingRights"

# SEBI's tree, and the shape of it matters: PUBLIC ALREADY CONTAINS the
# institutions. Adding promoter + institutions + public double-counts and
# lands past 100%, which is exactly what this view did before.
#   Promoter & promoter group   ─┐
#   Public                       ├ these two sum to 100
#     ├ Institutions - domestic  │
#     ├ Institutions - foreign   │
#     └ Non-institutions        ─┘
_AGG = {
    "promoter": "ShareholdingOfPromoterAndPromoterGroup_Context",
    "public": "PublicShareholding_Context",
    "inst_domestic": "InstitutionsDomestic_Context",
    "inst_foreign": "InstitutionsForeign_Context",
    "non_institutions": "NonInstitutions_Context",
    "non_promoter_non_public": "SharesHeldByNonPromoterNonPublicShareholders_Context",
}

# Which bucket a named holder belongs in, by its XBRL group tag.
_FOREIGN_GROUPS = (
    "InstitutionsForeignPortfolioInvestorCategoryOne",
    "InstitutionsForeignPortfolioInvestorCategoryTwo",
    "ForeignPortfolioInvestorsCategoryI", "ForeignPortfolioInvestorsCategoryII",
    "DetailsOfSharesHeldByOtherInstitutionsForeign", "OtherInstitutionsForeign",
    "ForeignVentureCapitalInvestors", "ForeignDirectInvestment",
    "SovereignWealthFunds", "ForeignNationals", "ForeignInstitutionalInvestors",
)
_DOMESTIC_GROUPS = (
    "MutualFundsOrUTI", "InsuranceCompanies", "Banks", "AlternativeInvestmentFunds",
    "ProvidentFundsOrPensionFunds", "NBFCRegisteredWithRBI", "VentureCapitalFunds",
    "AssetReconstructionCompanies", "OtherFinancialInstitutions",
    "InstitutionsDomestic", "OtherInstitutionsDomestic",
)
_PROMOTER_GROUPS = ("IndividualsOrHUF", "OthersIndianShareholders", "BodiesCorporate",
                    "Foreign", "CentralGovernmentOrStateGovernments", "AnyOther")


def _bucket(group_tag: str, is_promoter: bool) -> tuple[str, str]:
    """(bucket, label) for a named holder. Promoter flag wins - the same
    group tags are reused inside the promoter table."""
    if is_promoter:
        return "promoter", _pretty(group_tag)
    for g in _FOREIGN_GROUPS:
        if group_tag.startswith(g):
            return "inst_foreign", _pretty(group_tag)
    for g in _DOMESTIC_GROUPS:
        if group_tag.startswith(g):
            return "inst_domestic", _pretty(group_tag)
    if group_tag.startswith("CustodianOrDRHolder"):
        return "non_promoter_non_public", _pretty(group_tag)
    return "non_institutions", _pretty(group_tag)


def entity_kind(name: str) -> str:
    """What KIND of entity a holder name denotes. Read off the legal suffix,
    which Indian names carry reliably - the filing's PAN field is masked."""
    n = " " + name.upper().replace(".", "") + " "
    if " LLP " in n or "LIMITED LIABILITY PARTNERSHIP" in n:
        return "LLP"
    if "PRIVATE LIMITED" in n or " PVT LTD " in n or " PVT LIMITED " in n:
        return "Private company"
    if " LIMITED " in n or " LTD " in n or " PLC " in n:
        return "Company"
    if "TRUST" in n:
        return "Trust"
    if " HUF " in n or "KARTA" in n:
        return "HUF"
    if "MUTUAL FUND" in n or " MF " in n or "TRUSTEE" in n:
        return "Fund"
    if "ASSOCIATION" in n or " SOCIETY " in n:
        return "Association"
    if any(k in n for k in ("FUND", "INVESTMENT", "CAPITAL", "PENSION", "NPS")):
        return "Fund"
    return "Individual"


@dataclass
class Holder:
    name: str
    bucket: str           # promoter | inst_domestic | inst_foreign | non_institutions
    category: str         # the XBRL group, human-readable
    kind: str             # LLP | Company | Trust | Individual | Fund ...
    shares: int | None
    pct: float | None
    pledged_pct: float | None = None
    # Who ultimately controls this entity, from the filing's own Significant
    # Beneficial Owner declaration (Companies Act s.90). This is what turns a
    # wall of anonymous LLPs into a readable control chain.
    beneficial_owner: str = ""


@dataclass
class Shareholding:
    symbol: str
    as_of: str
    source_url: str
    totals: dict = field(default_factory=dict)      # bucket -> pct
    categories: dict = field(default_factory=dict)  # fine-grained label -> pct
    holders: list = field(default_factory=list)
    total_shares: int | None = None


_PDF_LOCK = threading.Lock()   # see fetch_report_text: PDFium is not thread-safe


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _pretty(tag: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", tag).replace(" Or ", "/").strip()


def latest_shareholding(symbol: str) -> Shareholding:
    """The most recent quarterly shareholding pattern, whole and unabridged."""
    sym = symbol.upper().replace(".NS", "")
    c = _client()
    try:
        try:
            rows = c.get(f"{_BASE}/api/corporate-share-holdings-master",
                         params={"index": "equities", "symbol": sym}).json()
        except Exception as exc:
            raise DataError(f"shareholding index unavailable for {sym}: {exc}") from exc
        if not isinstance(rows, list) or not rows:
            raise DataError(f"NSE lists no shareholding filings for {sym}")
        row = next((r for r in rows if r.get("xbrl")), None)
        if row is None:
            raise DataError(f"{sym}: filings exist but none carry XBRL")
        try:
            xml = c.get(row["xbrl"]).text
        except Exception as exc:
            raise DataError(f"XBRL fetch failed: {exc}") from exc
    finally:
        c.close()

    root = ET.fromstring(xml.encode())
    facts: dict[str, dict] = defaultdict(dict)
    for el in root:
        ref = el.get("contextRef")
        if ref and el.text and el.text.strip():
            facts[ref][el.tag.split("}")[-1]] = el.text.strip()

    def pct_of(f: dict):
        for t in (_PCT_SCRR, _PCT, _VOTING):
            if t in f:
                v = _num(f[t])
                if v is not None:
                    return round(v * 100, 4)
        return None

    # ---- aggregates -------------------------------------------------------
    totals, categories = {}, {}
    total_shares = None
    for key, prefix in _AGG.items():
        for ref, f in facts.items():
            if ref.startswith(prefix) and "NumberOfFullyPaidUpEquityShares" in f:
                v = pct_of(f)
                if v is not None:
                    totals[key] = round(v, 2)
                    break
    for ref, f in facts.items():
        if ref.startswith("D_") or "NameOfTheShareholder" in f:
            continue
        if "NumberOfFullyPaidUpEquityShares" not in f:
            continue
        v = pct_of(f)
        tag = ref.split("_Context")[0]
        if v and v >= 0.005 and tag not in _AGG.values():
            categories.setdefault(_pretty(tag), round(v, 2))
        if ref.startswith("TotalShareholding") or ref.startswith("Total_Context"):
            total_shares = int(_num(f["NumberOfFullyPaidUpEquityShares"]) or 0) or None

    # ---- every named holder, no cap ---------------------------------------
    holders: list[Holder] = []
    for ref, f in facts.items():
        name = f.get("NameOfTheShareholder")
        if not name or not ref.startswith("D_"):
            continue
        num_ref = ref[2:]
        nf = facts.get(num_ref, {})
        group_tag = num_ref.split("_Context")[0]
        is_prom = bool(f.get("TypeOfPromoterShareholding")) or "promoter" in group_tag.lower()
        bucket, label = _bucket(group_tag, is_prom)
        pledged = None
        for t in _PLEDGE_TAGS:
            if t in nf and "Percentage" in t:
                pledged = _num(nf[t])
                pledged = round(pledged * 100, 2) if pledged is not None else None
                break
        holders.append(Holder(
            name=name.strip(), bucket=bucket, category=label,
            kind=entity_kind(name),
            shares=int(_num(nf.get("NumberOfFullyPaidUpEquityShares")) or 0) or None,
            pct=pct_of(nf), pledged_pct=pledged,
        ))
    # ---- significant beneficial owners: registered entity -> real controller
    sbo: dict[str, str] = {}
    for f in facts.values():
        owner = f.get("NameOfSignificantBeneficialOwners")
        reg = f.get("NameOfRegisteredOwner")
        if owner and reg:
            sbo[reg.strip().upper()] = re.sub(r"\s{2,}", " ", owner.strip())
    for h in holders:
        h.beneficial_owner = sbo.get(h.name.upper(), "")

    holders.sort(key=lambda h: (h.pct is None, -(h.pct or 0)))

    sh = Shareholding(symbol=sym, as_of=row.get("date") or "",
                      source_url=row["xbrl"], totals=totals,
                      categories=categories, holders=holders,
                      total_shares=total_shares)
    try:
        _persist(sh)
    except Exception:
        pass     # the registry is a bonus; a write failure must not break a read
    return sh


def _persist(sh: Shareholding) -> None:
    """Append this filing to the local ownership registry.

    Every company scanned makes the REVERSE question answerable - which
    companies does LIC hold, and how much - without any new source. The
    index grows as the terminal is used."""
    import pandas as pd

    from shunkan.store.store import STORE_DIR

    d = STORE_DIR / "ownership"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "holders.parquet"
    fresh = pd.DataFrame([{
        "symbol": sh.symbol, "as_of": sh.as_of, "holder": h.name,
        "bucket": h.bucket, "category": h.category, "kind": h.kind,
        "shares": h.shares, "pct": h.pct, "pledged_pct": h.pledged_pct,
        "beneficial_owner": h.beneficial_owner,
    } for h in sh.holders])
    if fresh.empty:
        return
    if path.exists():
        try:
            old = pd.read_parquet(path)
            fresh = pd.concat([old, fresh], ignore_index=True)
        except Exception:
            path.rename(path.with_suffix(f".{int(__import__('time').time())}.corrupt.parquet"))
    fresh = fresh.drop_duplicates(subset=["symbol", "as_of", "holder"], keep="last")
    fresh.to_parquet(path, index=False)


def holder_positions(query: str, root=None) -> dict:
    """Reverse lookup: every company in the local registry this holder owns.

    Coverage is exactly what has been scanned - stated, never implied to be
    the holder's full book."""
    import pandas as pd

    from shunkan.store.store import STORE_DIR

    path = (root or STORE_DIR) / "ownership" / "holders.parquet"
    if not path.exists():
        return {"query": query, "rows": [], "companies_scanned": 0,
                "note": "no company has been scanned yet - open a few CMP pages"}
    df = pd.read_parquet(path)
    scanned = df["symbol"].nunique()
    hit = df[df["holder"].str.contains(re.escape(query), case=False, na=False)]
    hit = hit.sort_values("pct", ascending=False)
    return {
        "query": query,
        "companies_scanned": int(scanned),
        "matched_names": sorted(hit["holder"].unique().tolist())[:40],
        "rows": [{"symbol": r.symbol, "holder": r.holder, "pct": r.pct,
                  "shares": None if pd.isna(r.shares) else int(r.shares),
                  "as_of": r.as_of, "bucket": r.bucket}
                 for r in hit.head(200).itertuples()],
        "note": (f"local registry only: {scanned} companies scanned so far. "
                 "This is not the holder's full book - it is every position "
                 "visible in what this terminal has read."),
    }


# ---------------------------------------------------------------------------
# Annual reports
# ---------------------------------------------------------------------------


def annual_reports(symbol: str) -> list[dict]:
    """Every annual report NSE has for this symbol, newest first."""
    sym = symbol.upper().replace(".NS", "")
    c = _client()
    try:
        data = c.get(f"{_BASE}/api/annual-reports",
                     params={"index": "equities", "symbol": sym}).json()
    except Exception as exc:
        raise DataError(f"annual report index unavailable: {exc}") from exc
    finally:
        c.close()
    out = []
    for r in (data.get("data") or []):
        if not r.get("fileName"):
            continue
        out.append({"from_year": r.get("fromYr"), "to_year": r.get("toYr"),
                    "url": r["fileName"], "size": r.get("attFileSize"),
                    "filed": r.get("broadcast_dttm")})
    out.sort(key=lambda r: (r["to_year"] or ""), reverse=True)
    if not out:
        raise DataError(f"NSE lists no annual reports for {sym}")
    return out


def fetch_report_text(url: str, max_pages: int = 600) -> tuple[str, int]:
    """Extract text from an annual report PDF. Returns (text, pages_read).

    pypdfium2, not pypdf and not pymupdf, and the choice was measured on the
    Balrampur FY2026 report (400 pages, 16.6 MB):

        pypdf       7.2s   1,057,520 chars
        pymupdf     0.9s   1,059,889 chars   - but AGPL, and this package is MIT
        pypdfium2   0.4s   1,080,236 chars   - BSD/Apache, most text, fastest

    pypdfium2 also preserves the loose text labels inside flow diagrams, which
    matters more than it sounds: the densest supply-chain content in an Indian
    annual report is usually an infographic, and "Granular Potash Sold to
    Fertilizer Vendors" exists nowhere else in the document. Docling was tested
    too and DELETES those labels, emitting "<!-- image -->" for the figure.

    WHAT NO TEXT EXTRACTOR FIXES: these reports embed a font literally named
    ITF Rupee that maps the rupee glyph onto the codepoint H, so every parser
    reads "H450 crores". Numbers must come from XBRL, never from this text.

    Scanned/image-only reports yield little text, which the caller must report
    as a miss rather than paper over.
    """
    import io

    import httpx
    import pypdfium2 as pdfium

    try:
        raw = httpx.get(url, headers={"User-Agent": _H["User-Agent"]},
                        timeout=300.0, follow_redirects=True).content
    except Exception as exc:
        raise DataError(f"annual report download failed: {exc}") from exc

    # PDFium is NOT thread-safe, and it fails in a way that impersonates bad
    # input rather than a race. Running five bulk extractions in parallel gave
    # "PDFium: Data format error" for BAJAJ-AUTO and 4,494 characters from
    # ASIANPAINT's 294 pages - both of which parse perfectly on their own
    # (61,495 and 64,387 chars in the first 20 pages). Read as a data problem
    # those look like corrupt or scanned filings, and the honest-refusal path
    # would have recorded them as such and moved on, seeding the database with
    # a permanent hole. Serialise instead.
    #
    # The cost is nil: parsing is 0.4s and the LLM call it feeds is ~170s, so
    # callers keep essentially all of their concurrency.
    with _PDF_LOCK:
        try:
            doc = pdfium.PdfDocument(io.BytesIO(raw))
        except Exception as exc:
            raise DataError(f"annual report is not readable as PDF: {exc}") from exc
        chunks = []
        n = min(len(doc), max_pages)
        for i in range(n):
            try:
                chunks.append(doc[i].get_textpage().get_text_range() or "")
            except Exception:
                continue  # one unreadable page never costs the other 399
    return "\n".join(chunks), n


def registry_stats(root=None) -> dict:
    """What the local ownership registry actually contains."""
    import pandas as pd

    from shunkan.store.store import STORE_DIR

    path = (root or STORE_DIR) / "ownership" / "holders.parquet"
    if not path.exists():
        return {"companies": 0, "holders": 0, "rows": 0}
    try:
        df = pd.read_parquet(path)
    except Exception:
        return {"error": "registry unreadable"}
    return {"companies": int(df["symbol"].nunique()),
            "holders": int(df["holder"].nunique()),
            "rows": int(len(df)),
            "as_of_dates": sorted(df["as_of"].dropna().unique().tolist())[-3:]}


# ---------------------------------------------------------------------------
# The rest of the mandatory disclosure surface
# ---------------------------------------------------------------------------
#
# Everything below is filed under a regulation that compels it, which is why
# it exists at all and why it can be trusted: PIT Reg 7 (insider dealing),
# LODR Reg 29/33 (board meetings and results), LODR Reg 30 (announcements),
# SEBI's credit-rating disclosure, and SAST Reg 31 (pledge). A terminal that
# reads the shareholding pattern and stops is leaving most of the compelled
# record on the table.


def _rows(path: str, params: dict) -> list:
    c = _client()
    try:
        d = c.get(f"{_BASE}{path}", params=params).json()
    except Exception as exc:
        raise DataError(f"{path} unavailable: {str(exc)[:110]}") from exc
    finally:
        c.close()
    if isinstance(d, dict):
        d = d.get("data", [])
    return d if isinstance(d, list) else []


def insider_trades(symbol: str, limit: int = 40) -> list[dict]:
    """PIT Reg 7 disclosures: who inside the company bought or sold.

    Source: NSE /api/corporates-pit, the exchange's own filing of SEBI
    (Prohibition of Insider Trading) Regulations 2015, Reg 7(2) - a
    designated person, promoter or immediate relative must disclose any
    trade above Rs 10 lakh within two trading days. It is compelled
    disclosure, not a vendor feed.

    The signal is the PERSON CATEGORY, not the size - a promoter buying is a
    different fact from an employee exercising ESOPs, and the filing
    distinguishes them.

    FIELD TRAP, measured against RELIANCE on 2026-08-25: buyQuantity and
    sellquantity are '0' in 20 of 20 rows. The real transaction size lives in
    secAcq, and it reconciles: 3920 (befAcqSharesNo) - 2320 (secAcq) = 1600
    (afterAcqSharesNo). Reading the buy/sell pair rendered a column of zeros
    that looked like data and was not.

    The percentage fields are a second trap. befAcqSharesPer is '0' for 14 of
    20 rows - not because the person owns nothing, but because their holding
    rounds to zero against a 676-crore share count. Nasib Kapoor holds 2,500
    shares and the feed calls it 0%. So percentages are returned ONLY when
    non-zero, and the SHARE COUNTS are returned alongside, because those are
    the numbers the filing actually asserts.
    """
    sym = symbol.upper().replace(".NS", "")
    out = []
    for r in _rows("/api/corporates-pit", {"index": "equities", "symbol": sym})[:limit]:
        # 'Nil' is how this feed says zero holding; _num would return None and
        # the caller could not tell it apart from a missing field.
        def _shares(v):
            if str(v).strip().lower() in ("nil", "-", "", "none"):
                return 0.0
            return _num(v)

        qty = _num(r.get("secAcq"))
        if not qty:   # older rows do populate the buy/sell pair
            qty = (_num(r.get("buyQuantity")) or 0) or (
                _num(r.get("sellquantity") or r.get("sellQuantity")) or 0) or None
        pct_b, pct_a = _num(r.get("befAcqSharesPer")), _num(r.get("afterAcqSharesPer"))
        out.append({
            "date": r.get("date"),
            "name": r.get("acqName"),
            "category": r.get("personCategory"),
            "mode": r.get("acqMode"),
            "type": r.get("tdpTransactionType"),
            "security": r.get("secType"),
            "qty": qty,
            "value": _num(r.get("secVal")),
            "shares_before": _shares(r.get("befAcqSharesNo")),
            "shares_after": _shares(r.get("afterAcqSharesNo")),
            # a rounded-to-zero percentage is not a fact about ownership
            "pct_before": pct_b or None,
            "pct_after": pct_a or None,
            "xbrl": r.get("xbrl") or None,
        })
    return out


def board_meetings(symbol: str, limit: int = 12) -> list[dict]:
    """LODR Reg 29 intimations - which is where an earnings DATE comes from.

    The company view used to refuse an earnings calendar for want of a
    source. This is the source: the company itself telling the exchange
    when its board will consider results."""
    sym = symbol.upper().replace(".NS", "")
    out = []
    for r in _rows("/api/corporate-board-meetings", {"index": "equities", "symbol": sym})[:limit]:
        desc = str(r.get("bm_desc") or "")
        out.append({
            "date": r.get("bm_date"),
            "purpose": r.get("bm_purpose"),
            "description": desc[:300],
            "is_results": bool(re.search(r"result|financial", desc + str(r.get("bm_purpose")), re.I)),
        })
    return out


def corporate_actions(symbol: str, limit: int = 15) -> list[dict]:
    """Dividends, splits, bonuses - with the ex-date that actually matters."""
    sym = symbol.upper().replace(".NS", "")
    out = []
    for r in _rows("/api/corporates-corporateActions", {"index": "equities", "symbol": sym})[:limit]:
        out.append({"ex_date": r.get("exDate"), "purpose": r.get("subject"),
                    "record_date": r.get("recDate"), "series": r.get("series")})
    return out


def credit_ratings(symbol: str, limit: int = 10) -> list[dict]:
    """Rating actions. NSE serves a market-wide feed here, so it is filtered
    to the company by name - an unfiltered feed would attribute another
    issuer's downgrade to this one."""
    sym = symbol.upper().replace(".NS", "")
    out = []
    for r in _rows("/api/corporate-credit-rating", {"index": "equities", "symbol": sym}):
        if str(r.get("Symbol", "")).upper() not in (sym, ""):
            continue
        out.append({"date": r.get("DateofCR"), "agency": r.get("NameOfCRAgency"),
                    "rating": r.get("CreditRating"), "action": r.get("RatingAction"),
                    "subject": r.get("Subject")})
        if len(out) >= limit:
            break
    return out


def promoter_pledge(symbol: str) -> dict | None:
    """SAST Reg 31 encumbrance. Zero is a real and good answer; the absence
    of a filing is not the same as zero and is reported as unknown."""
    sym = symbol.upper().replace(".NS", "")
    rows = _rows("/api/corporate-pledgedata", {"index": "equities", "symbol": sym})
    if not rows:
        return None
    r = rows[0]
    return {"as_of": r.get("broadcastDt"),
            "pledged_shares": _num(r.get("noOfPledgeShare")),
            "promoter_nbfc_shares": _num(r.get("nbfcPromoShare")),
            "company": r.get("comName")}


def announcements(symbol: str, limit: int = 25) -> list[dict]:
    """LODR Reg 30 - the running record of everything material."""
    sym = symbol.upper().replace(".NS", "")
    out = []
    for r in _rows("/api/corporate-announcements", {"index": "equities", "symbol": sym})[:limit]:
        out.append({"date": r.get("an_dt") or r.get("sort_date"),
                    "subject": (r.get("desc") or r.get("subject") or "")[:160],
                    "detail": (r.get("attchmntText") or "")[:400],
                    "attachment": r.get("attchmntFile")})
    return out


def quarterly_results(symbol: str, limit: int = 8) -> list[dict]:
    """Ind AS quarterly filings, newest first, with their XBRL link.

    Yahoo serves ANNUAL statements only; the exchange has every quarter,
    and the XBRL behind each one carries segment reporting (Ind AS 108)."""
    sym = symbol.upper().replace(".NS", "")
    rows = _rows("/api/corporates-financial-results",
                 {"index": "equities", "symbol": sym, "period": "Quarterly"})

    def key(r):
        try:
            return datetime.strptime(str(r.get("broadCastDate"))[:11], "%d-%b-%Y")
        except Exception:
            return datetime(1970, 1, 1)

    rows.sort(key=key, reverse=True)
    return [{"from": r.get("fromDate"), "to": r.get("toDate"),
             "financial_year": r.get("financialYear"),
             "basis": r.get("consolidated"), "audited": r.get("audited"),
             "filed": r.get("broadCastDate"), "xbrl": r.get("xbrl")}
            for r in rows[:limit]]
