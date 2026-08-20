"""Index constituent lists, from NSE's own published CSVs.

Two jobs. First, the authoritative answer to "which companies are in NIFTY 50
and BANKNIFTY", from
https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv (and the
bank equivalent), which also carries the full company name - the thing a
headline actually says. Second, an alias table for mapping headlines to
symbols, built from those names plus a short explicit list of the forms the
press actually uses (SBI, L&T, TCS).

The alias rules exist because Indian corporate names are a minefield for
substring matching. "Kotak Mahindra Bank" contains "Mahindra"; "Tech Mahindra"
and "Mahindra & Mahindra" are different companies; "LT" is a stock symbol and
an abbreviation for nothing a journalist writes. So: aliases are matched
longest-first, against the TITLE only, and a company's alias is its cleaned
full name plus explicit extras - never a bare fragment and never the symbol
itself unless the symbol is what the press writes (ITC, TCS, ONGC).

Constituents change at index rebalances (quarterly-ish), so the cache is
short-lived and the fetch is re-run by the news loop rather than trusted
forever.
"""

from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass

import pandas as pd

from shunkan.data.provider import DataError

INDEX_FILES = {
    "NIFTY50": "ind_nifty50list.csv",
    "BANKNIFTY": "ind_niftybanklist.csv",
    "NIFTYNEXT50": "ind_niftynext50list.csv",
    "NIFTY100": "ind_nifty100list.csv",
    "NIFTY200": "ind_nifty200list.csv",
    "NIFTY500": "ind_nifty500list.csv",
    "MIDCAP150": "ind_niftymidcap150list.csv",
    "SMALLCAP250": "ind_niftysmallcap250list.csv",
}
ARCHIVE = "https://nsearchives.nseindia.com/content/indices/"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}

_CACHE_TTL = 7 * 24 * 3600.0  # rebalances are quarterly; a week is plenty

# Forms the press writes that the CSV's legal name does not cover, plus the
# handful of cases where the cleaned name itself would be a trap.
_EXTRA_ALIASES: dict[str, tuple[str, ...]] = {
    "SBIN": ("SBI", "State Bank"),
    "LT": ("L&T", "Larsen and Toubro"),
    "TCS": ("TCS",),
    "ONGC": ("ONGC",),
    "ITC": ("ITC",),
    "HCLTECH": ("HCLTech", "HCL Tech"),
    "M&M": ("M&M",),
    "NTPC": ("NTPC",),
    "SBILIFE": ("SBI Life",),
    "HDFCLIFE": ("HDFC Life",),
    "BAJAJ-AUTO": ("Bajaj Auto",),
    "ULTRACEMCO": ("UltraTech",),
    "ADANIENT": ("Adani Enterprises",),
    "ADANIPORTS": ("Adani Ports",),
}

_SUFFIX = re.compile(r"\s+(ltd\.?|limited)\s*$", re.I)


@dataclass(frozen=True)
class Constituent:
    symbol: str
    name: str            # full name as NSE publishes it
    indices: tuple[str, ...]
    industry: str = ""   # NSE's own Industry column; sector grouping for news


def _clean_name(name: str) -> str:
    return _SUFFIX.sub("", name.strip()).strip().rstrip(".")


def parse_constituents_csv(text: str, index_name: str) -> list[Constituent]:
    """Pure parser, unit-testable offline. Refuses on a changed format."""
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip() for c in df.columns]
    need = {"Company Name", "Symbol"}
    if not need <= set(df.columns):
        raise DataError(f"constituent CSV for {index_name} missing {need - set(df.columns)}")
    return [Constituent(symbol=str(r["Symbol"]).strip().upper(),
                        name=str(r["Company Name"]).strip(),
                        indices=(index_name,),
                        industry=(str(r["Industry"]).strip()
                                  if "Industry" in df.columns
                                  and not pd.isna(r.get("Industry")) else ""))
            for _, r in df.iterrows()]


_mem: dict[str, tuple[float, list[Constituent]]] = {}


def fetch_constituents(index_name: str) -> list[Constituent]:
    import httpx

    now = time.time()
    hit = _mem.get(index_name)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    fname = INDEX_FILES[index_name]
    try:
        r = httpx.get(ARCHIVE + fname, headers=_HEADERS, timeout=15.0)
        r.raise_for_status()
    except Exception as exc:
        if hit:
            return hit[1]        # stale beats absent, and the staleness is days
        raise DataError(f"constituent list {fname} unreachable: {exc}") from exc
    out = parse_constituents_csv(r.text, index_name)
    _mem[index_name] = (now, out)
    return out


def universe(indices: tuple[str, ...] = ("NIFTY50", "BANKNIFTY")) -> list[Constituent]:
    """The union, with membership merged for symbols in both."""
    by_sym: dict[str, Constituent] = {}
    for idx in indices:
        for c in fetch_constituents(idx):
            prev = by_sym.get(c.symbol)
            merged = tuple(sorted(set((prev.indices if prev else ()) + c.indices)))
            industry = c.industry or (prev.industry if prev else "")
            by_sym[c.symbol] = Constituent(c.symbol, c.name, merged, industry)
    return sorted(by_sym.values(), key=lambda c: c.symbol)


def alias_table(constituents: list[Constituent]) -> list[tuple[str, str]]:
    """(alias, symbol) pairs, longest alias first.

    Longest-first is the collision defence: "Kotak Mahindra Bank" must claim
    its title before any shorter Mahindra alias could, and "Tech Mahindra"
    before "Mahindra & Mahindra" is even considered. A alias shorter than 3
    characters never enters the table at all.
    """
    pairs: list[tuple[str, str]] = []
    for c in constituents:
        aliases = {_clean_name(c.name)}
        aliases.update(_EXTRA_ALIASES.get(c.symbol, ()))
        for a in aliases:
            if len(a) >= 3:
                pairs.append((a, c.symbol))
    return sorted(pairs, key=lambda p: -len(p[0]))


def map_title(title: str, aliases: list[tuple[str, str]]) -> list[str]:
    """Symbols whose alias appears in the TITLE, word-boundary matched.

    Title only, deliberately: Google's query matching sees body text too,
    which is how a Women's Day listicle arrives in a Reliance query. If the
    headline does not name the company, it is not tagged with it.
    """
    hits: list[str] = []
    low = title.lower()
    for alias, sym in aliases:
        if sym in hits:
            continue
        pat = r"(?<![a-z0-9])" + re.escape(alias.lower()) + r"(?![a-z0-9])"
        if re.search(pat, low):
            hits.append(sym)
    return hits


def industry_map(constituents: list[Constituent]) -> dict[str, str]:
    """symbol -> NSE Industry, for grouping tagged headlines by sector."""
    return {c.symbol: c.industry for c in constituents if c.industry}
