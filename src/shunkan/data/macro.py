"""India macro: the policy corridor and the long-run series behind it.

WHY THIS EXISTS. Shunkan knows a great deal about 500 Indian companies and
nothing at all about the economy they trade in - which is backwards for a
derivatives terminal, because the single most important input to an option
price after the underlying is the risk-free rate, and the single most
important driver of that rate is the MPC's next decision. A desk that can
price a straddle but cannot say what the repo rate is has a hole in it.

TWO SOURCES, DELIBERATELY UNBLENDED, because they differ in kind:

  RBI, scraped from its own homepage. The policy corridor as the central bank
  publishes it - repo, SDF, MSF, bank rate, CRR, SLR. Authoritative and
  current: this IS the number, not a vendor's copy of it.

  World Bank, over its free JSON API. Long-run annual series - inflation,
  reserves, current account, unemployment. Not current enough to trade on and
  not pretending to be; it is the decade of context behind the corridor.

SCRAPING IS FRAGILE AND MUST FAIL LOUDLY. RBI can restyle its homepage
whenever it likes, and the failure mode of a rate scraper is not an exception
- it is a plausible-looking stale number. So every rate carries the label it
was found under, a miss is a refusal that names itself, and NOTHING here ever
falls back to a hardcoded "typical" value. A macro screen that invents a repo
rate is worse than one that says it could not read the page.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field

from shunkan.data.memcache import _SENTINEL as _CACHE_MISS
from shunkan.data.memcache import TTLCache, ttl_cache
from shunkan.data.provider import DataError

RBI_URL = "https://www.rbi.org.in/"
WB_URL = "https://api.worldbank.org/v2/country/IND/indicator/{ind}"

_UA = {"User-Agent": "Mozilla/5.0 (compatible; Shunkan/0.1; +local research)"}

# Label as RBI writes it -> (our key, human name, what it means).
# Ordered as the corridor is usually read: the policy rate, then the floor and
# ceiling that bracket it, then the reserve ratios.
_RATES: tuple[tuple[str, str, str, str], ...] = (
    ("Policy Repo Rate", "repo", "POLICY REPO",
     "the rate the MPC sets; every other short rate is priced off it"),
    ("Standing Deposit Facility", "sdf", "SDF (FLOOR)",
     "banks park surplus here — the floor of the corridor"),
    ("Marginal Standing Facility", "msf", "MSF (CEILING)",
     "emergency borrowing — the ceiling of the corridor"),
    ("Bank Rate", "bank_rate", "BANK RATE",
     "the rate on RBI's discounting of eligible paper"),
    ("Fixed Reverse Repo", "reverse_repo", "REVERSE REPO",
     "retained in the corridor but no longer the operative floor"),
    ("Cash Reserve Ratio", "crr", "CRR",
     "share of deposits held with RBI, earning nothing"),
    ("Statutory Liquidity Ratio", "slr", "SLR",
     "share of deposits held in government securities"),
)

# Aliases, because the homepage abbreviates inconsistently between refreshes.
_ALIASES: dict[str, tuple[str, ...]] = {
    "sdf": ("Standing Deposit Facility Rate", "SDF Rate", "SDF"),
    "msf": ("Marginal Standing Facility Rate", "MSF Rate", "MSF"),
    "crr": ("CRR",),
    "slr": ("SLR",),
    "reverse_repo": ("Reverse Repo Rate", "Fixed Reverse Repo Rate"),
    "repo": ("Repo Rate",),
}

# Curated because a picker over 1,400 World Bank indicators is a research
# project, not a screen. These are the ones an equity desk actually reads.
INDICATORS: tuple[tuple[str, str, str, str], ...] = (
    ("FP.CPI.TOTL.ZG", "CPI INFLATION", "%",
     "what the MPC is actually targeting — the 4% ±2 band"),
    ("NY.GDP.MKTP.KD.ZG", "REAL GDP GROWTH", "%",
     "the other half of the MPC's mandate"),
    ("FI.RES.TOTL.CD", "FX RESERVES", "USD",
     "the buffer behind the rupee; drives RBI's room to defend it"),
    ("BN.CAB.XOKA.GD.ZS", "CURRENT ACCOUNT", "% GDP",
     "external deficit — the structural pressure on the rupee"),
    ("SL.UEM.TOTL.ZS", "UNEMPLOYMENT", "%",
     "slack in the economy, ILO-modelled"),
    ("NE.GDI.TOTL.ZS", "GROSS CAPITAL FORMATION", "% GDP",
     "the investment cycle that capex-heavy sectors trade on"),
)


@dataclass
class Rate:
    key: str
    label: str
    pct: float | None
    found_as: str | None
    note: str
    reason: str | None = None       # why it is missing, when it is


@dataclass
class MacroSeries:
    indicator: str
    label: str
    unit: str
    note: str
    points: list[dict] = field(default_factory=list)
    reason: str | None = None


def _offline() -> bool:
    return os.environ.get("SHUNKAN_OFFLINE", "").strip() in {"1", "true", "yes"}


def _get(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310
        return r.read().decode("utf-8", errors="ignore")


def _flatten(html: str) -> str:
    """Tags become a SPACE, then whitespace collapses.

    This started as a pipe, on the reasoning that a visible delimiter stops a
    label welding to the next cell's number. Tested against the real page,
    pipe, space and empty string all read the same seven rates - so that
    reasoning demonstrated nothing, and the choice had to be settled on the
    cases where they actually differ. Two are realistic and the pipe loses
    both:

        "Policy <b>Repo</b> Rate"     pipe -> "Policy |Repo| Rate", no match
        "<td>5.25</td><td>%</td>"     pipe -> "5.25|%", no match

    A space rejoins a label split by inline markup, still lets the number
    regex span a cell boundary, and keeps distinct cells apart. That is the
    opposite of what the first version of this comment asserted.
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"[ \t]+", " ", text)


def _find_rate(flat: str, key: str, label: str, note: str) -> Rate:
    """Read one rate, or say why not.

    The percentage must appear WITHIN A SHORT WINDOW of its label. An
    unbounded search would happily pair "Policy Repo Rate" with a number from
    a press release three screens away - which is exactly how a scraper starts
    reporting a rate that was true in 2019.
    """
    names = (label,) + _ALIASES.get(key, ())
    for name in names:
        for m in re.finditer(re.escape(name), flat, flags=re.I):
            window = flat[m.end():m.end() + 90]
            hit = re.search(r"(\d{1,2}(?:\.\d{1,2})?)\s*%", window)
            if hit:
                return Rate(key=key, label=label, pct=float(hit.group(1)),
                            found_as=name, note=note)
    return Rate(key=key, label=label, pct=None, found_as=None, note=note,
                reason=f"RBI's page carries no percentage within 90 characters "
                       f"of {label!r} — the layout likely changed")


@ttl_cache(ttl=3600.0, max_items=4)
def policy_rates() -> dict:
    """The RBI policy corridor, read from RBI's own homepage."""
    if _offline():
        raise DataError("offline mode (SHUNKAN_OFFLINE=1) — the policy "
                        "corridor is read live from rbi.org.in")
    try:
        html = _get(RBI_URL)
    except Exception as exc:                                   # noqa: BLE001
        raise DataError(f"could not reach rbi.org.in — {type(exc).__name__}: "
                        f"{str(exc)[:90]}") from exc
    flat = _flatten(html)
    rates = [_find_rate(flat, key, label, note)
             for label, key, _disp, note in
             [(lbl, k, d, n) for lbl, k, d, n in _RATES]]
    got = [r for r in rates if r.pct is not None]
    if not got:
        # Every single label missed. That is a changed page, not a changed
        # policy, and reporting an empty corridor as fact would be a lie.
        raise DataError(
            "read rbi.org.in but found none of the seven policy rates — "
            "the page structure has changed and the parser needs updating")
    corridor = {r.key: r.pct for r in got}
    width = None
    if "msf" in corridor and "sdf" in corridor:
        width = round(corridor["msf"] - corridor["sdf"], 2)
    return {
        "rates": [r.__dict__ for r in rates],
        "corridor_width_bps": None if width is None else round(width * 100),
        "source": RBI_URL,
        "source_note": "Reserve Bank of India, read from its published "
                       "homepage rates table",
        "found": len(got), "expected": len(_RATES),
    }


# Successful series only. A REFUSAL MUST NEVER BE CACHED: series() returns
# its failures as a MacroSeries carrying a reason rather than raising, so the
# plain @ttl_cache that used to sit here stored them like any other value -
# and one transient timeout blanked the macro screen for six hours. Caught by
# exactly that: a blip during development, then a second call returning the
# same failure in 0.0s. Exceptions were never the problem; returned failures
# were.
_SERIES_CACHE = TTLCache(ttl=6 * 3600.0, max_items=32)


def series(indicator: str, start: int = 2005, end: int = 2026) -> MacroSeries:
    """One World Bank annual series for India."""
    meta = next((i for i in INDICATORS if i[0] == indicator), None)
    if meta is None:
        raise DataError(f"{indicator} is not one of the curated India "
                        f"indicators")
    _, label, unit, note = meta
    if _offline():
        return MacroSeries(indicator, label, unit, note,
                           reason="offline mode (SHUNKAN_OFFLINE=1)")
    ckey = (indicator, start, end)
    hit = _SERIES_CACHE.get(ckey)
    if hit is not _CACHE_MISS:
        return hit
    url = (WB_URL.format(ind=indicator)
           + f"?format=json&per_page=200&date={start}:{end}")
    payload = None
    last: Exception | None = None
    for attempt in range(2):        # one retry: a blip must not become a gap
        try:
            # 8s, not the default 20: this host answers in ~0.3s when it is
            # healthy, so a long timeout only lengthens the failure case.
            payload = json.loads(_get(url, timeout=8.0))
            break
        except Exception as exc:                               # noqa: BLE001
            last = exc
    if payload is None:
        return MacroSeries(indicator, label, unit, note,
                           reason=f"World Bank API unreachable after 2 "
                                  f"attempts — {type(last).__name__}")
    if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
        return MacroSeries(indicator, label, unit, note,
                           reason="World Bank returned no observations for "
                                  "India on this indicator")
    pts = [{"year": int(row["date"]), "value": float(row["value"])}
           for row in payload[1]
           if row.get("value") is not None and str(row.get("date", "")).isdigit()]
    pts.sort(key=lambda p: p["year"])
    if not pts:
        return MacroSeries(indicator, label, unit, note,
                           reason="every observation in the requested window "
                                  "is null at the source")
    out = MacroSeries(indicator, label, unit, note, points=pts)
    _SERIES_CACHE.put(ckey, out)        # only on success
    return out


def dashboard(start: int = 2005, end: int = 2026) -> dict:
    """Everything the macro screen needs, in one call.

    A failure in one block must not take the others with it: the corridor and
    the long-run series come from different institutions over different
    protocols, and RBI being unreachable says nothing about the World Bank.
    """
    out: dict = {"source_note": {
        "corridor": "Reserve Bank of India — the rates as RBI publishes them",
        "series": "World Bank open data, annual frequency — context, not a "
                  "trading signal",
    }}
    try:
        out["corridor"] = policy_rates()
    except DataError as exc:
        out["corridor"] = None
        out["corridor_reason"] = str(exc)
    # Fetched CONCURRENTLY. Six independent HTTP calls run back to back turn
    # one slow host into six slow requests, and with a retry each that is a
    # 40-second page load for a screen whose data changes once a year. They
    # share nothing, so there is nothing to serialise them for.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(INDICATORS)) as pool:
        futures = [pool.submit(series, ind, start, end)
                   for ind, *_ in INDICATORS]
        out["series"] = [f.result().__dict__ for f in futures]
    return out
