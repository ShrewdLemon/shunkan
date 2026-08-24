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
import xml.etree.ElementTree as ET
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
_PCT_TAGS = (
    "ShareholdingAsAPercentageOfTotalNumberOfSharesCalculatedAsPerSCRR1957AsAPercentageOfAPlusBPlusC2",
    "ShareholdingAsAPercentageAssumingFullConversionOfConvertibleSecuritiesAsAPercentageOfDilutedShareCapital",
    "ShareholdingAsAPercentageOfTotalNumberOfShares",
)
_PLEDGE_TAGS = (
    "SharesPledgedOrOtherwiseEncumberedAsAPercentageOfTotalShares",
    "NumberOfSharesPledgedOrOtherwiseEncumbered",
)


@dataclass
class Holder:
    name: str
    group: str            # promoter | institution | public
    category: str         # the XBRL group tag, human-readable
    shares: int | None
    pct: float | None
    pledged_pct: float | None = None


@dataclass
class Shareholding:
    symbol: str
    as_of: str
    source_url: str
    promoter_pct: float | None
    public_pct: float | None
    categories: dict = field(default_factory=dict)   # label -> pct
    holders: list[Holder] = field(default_factory=list)


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _group_of(tag: str) -> str:
    t = tag.lower()
    if "promoter" in t or t.startswith(("individualsorhuf", "othersindian",
                                        "bodiescorporate", "anyother")):
        return "promoter"
    if any(k in t for k in ("mutualfund", "institution", "insurance", "bank",
                            "alternativeinvestment", "foreignportfolio",
                            "provident", "sovereign", "assetreconstruction")):
        return "institution"
    return "public"


def _pretty(tag: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", tag).replace("Or", "/").strip()


def latest_shareholding(symbol: str) -> Shareholding:
    """The most recent quarterly shareholding pattern, parsed from XBRL."""
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
        for t in _PCT_TAGS:
            if t in f:
                return _num(f[t])
        return None

    holders: list[Holder] = []
    for ref, f in facts.items():
        name = f.get("NameOfTheShareholder")
        if not name or not ref.startswith("D_"):
            continue
        num_ref = ref[2:]
        nf = facts.get(num_ref, {})
        grp_tag = ref[2:].split("_Context")[0]
        pledged = None
        for t in _PLEDGE_TAGS:
            if t in nf and "Percentage" in t:
                pledged = _num(nf[t])
                break
        holders.append(Holder(
            name=name,
            group=("promoter" if f.get("TypeOfPromoterShareholding")
                   or "promoter" in grp_tag.lower() else _group_of(grp_tag)),
            category=_pretty(grp_tag),
            shares=int(_num(nf.get("NumberOfFullyPaidUpEquityShares")) or 0) or None,
            pct=(lambda v: round(v * 100, 2) if v is not None else None)(pct_of(nf)),
            pledged_pct=pledged,
        ))
    holders.sort(key=lambda h: (h.pct is None, -(h.pct or 0)))

    def cat(ctx_key: str):
        # Scan EVERY matching context, not just the first: several share the
        # prefix and the earliest one often carries counts without a
        # percentage, which silently blanked the public shareholding.
        for ref, f in facts.items():
            if ref.startswith(ctx_key):
                v = pct_of(f)
                if v is not None:
                    return v
        return None

    categories = {}
    for label, key in (("Promoter & promoter group", "ShareholdingOfPromoterAndPromoterGroup_Context"),
                       ("Public", "PublicShareholding_Context"),
                       ("Mutual funds / UTI", "MutualFundsOrUTI_Context"),
                       ("Foreign portfolio investors", "ForeignPortfolioInvestorsCategoryI_Context"),
                       ("Insurance companies", "InsuranceCompanies_Context"),
                       ("Alternative investment funds", "AlternativeInvestmentFunds_Context"),
                       ("Banks", "Banks_Context")):
        v = cat(key)
        if v is not None:
            # XBRL reports these as fractions of one; the whole app talks
            # percent, and a 0.43 on an ownership bar reads as 0.43%.
            categories[label] = round(v * 100, 2)

    return Shareholding(
        symbol=sym,
        as_of=row.get("date") or "",
        source_url=row["xbrl"],
        promoter_pct=categories.get("Promoter & promoter group"),
        public_pct=categories.get("Public"),
        categories=categories,
        holders=holders,
    )


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


def fetch_report_text(url: str, max_pages: int = 400) -> tuple[str, int]:
    """Extract text from an annual report PDF. Returns (text, pages_read).

    Annual reports run 200-400 pages and 15-40 MB; this reads them once and
    the caller caches. Scanned/image-only reports yield little text, which
    the caller must report as a miss rather than paper over."""
    import io

    import httpx
    from pypdf import PdfReader

    try:
        raw = httpx.get(url, headers={"User-Agent": _H["User-Agent"]},
                        timeout=180.0, follow_redirects=True).content
    except Exception as exc:
        raise DataError(f"annual report download failed: {exc}") from exc
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:
        raise DataError(f"annual report is not readable as PDF: {exc}") from exc
    chunks = []
    n = min(len(reader.pages), max_pages)
    for i in range(n):
        try:
            chunks.append(reader.pages[i].extract_text() or "")
        except Exception:
            continue
    return "\n".join(chunks), n
