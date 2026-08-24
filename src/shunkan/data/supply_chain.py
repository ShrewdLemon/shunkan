"""Supply-chain mapping from annual reports: SPLC, evidence-first.

Bloomberg's SPLC draws supplier and customer arrows from licensed data.
No such feed exists for NSE names, so this builds the map from the one
source that is both free and authoritative: the company's own annual
report, filed with the exchange.

THE RULE THAT SHAPES THIS FILE: every node on the map carries the
sentence it came from. Nothing is inferred, summarised, or supplied from
what a language model happens to know about the company - if the report
does not say it, the map does not show it. That makes the output
narrower than Bloomberg's and impossible to quietly fabricate, which is
the trade this codebase always makes.

What it extracts:
  INPUTS      what the company buys or consumes (raw materials, feedstock)
  OPERATIONS  plants, capacities, locations
  OUTPUTS     what it makes and sells
  CUSTOMERS   who buys it, where it goes, exports
  FAMILY      subsidiaries, joint ventures, associates

Detection is pattern-anchored, not vocabulary-bound: sentences are
matched on the phrases Indian annual reports actually use ("raw
material", "procurement of", "supplied to", "our customers", "exports
to", "wholly-owned subsidiary"), then the commodity or counterparty is
read out of that sentence. A curated commodity list only RANKS what was
found; it never invents a node.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field

# Commodity and product words worth surfacing when a sentence mentions them.
# This list ranks and labels; it never creates a node on its own.
_COMMODITY = {
    "sugarcane", "cane", "sugar", "molasses", "bagasse", "ethanol", "alcohol",
    "crude oil", "naphtha", "natural gas", "polymer", "polyester", "paraxylene",
    "iron ore", "coal", "coking coal", "limestone", "clinker", "cement",
    "steel", "aluminium", "copper", "zinc", "gypsum", "fly ash", "petcoke",
    "palm oil", "wheat", "barley", "milk", "cotton", "yarn", "pulp", "paper",
    "caustic soda", "soda ash", "ammonia", "urea", "phosphate", "potash",
    "silicon", "lithium", "rubber", "titanium", "gold", "silver", "platinum",
    "api", "excipient", "solvent", "resin", "chemicals", "packaging",
    "electricity", "power", "biomass", "hydrogen", "lactic acid", "pla",
}
_CUSTOMER_MARKERS = (
    r"oil marketing compan", r"OMCs?\b", r"our customers", r"customers include",
    r"supplied? to", r"sold to", r"sales to", r"end[- ]use", r"end customers",
    r"offtake", r"dealers?\b", r"distributors?\b", r"institutional buyers",
    r"exports? to", r"export markets?", r"B2B", r"retail customers",
)
_INPUT_MARKERS = (
    r"raw material", r"procurement of", r"procured from", r"feedstock",
    r"consumption of", r"purchases? of", r"sourced from", r"supplier",
    r"input cost", r"crushing", r"key inputs",
)
_FAMILY_MARKERS = (
    r"wholly[- ]owned subsidiar", r"\bsubsidiar(?:y|ies)\b", r"joint venture",
    r"associate compan", r"step[- ]down subsidiar",
)
_CAPACITY = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(MW|KLPD|KL per day|TCD|tonnes? per day|MTPA|"
    r"million tonnes?|MMTPA|MMSCMD|units?|plants?|factories|facilities)\b", re.I)
_STATES = (
    "Uttar Pradesh", "Maharashtra", "Gujarat", "Karnataka", "Tamil Nadu",
    "Andhra Pradesh", "Telangana", "Madhya Pradesh", "Rajasthan", "Bihar",
    "West Bengal", "Odisha", "Punjab", "Haryana", "Kerala", "Assam",
    "Jharkhand", "Chhattisgarh", "Uttarakhand", "Himachal Pradesh", "Goa",
    "Jamnagar", "Dahej", "Hazira", "Vadodara", "Nagothane", "Barabanki",
)
_NOISE = re.compile(r"^(page|annual report|contents|notice|corporate overview)", re.I)


@dataclass
class Node:
    term: str
    kind: str                 # input | output | customer | facility | family
    mentions: int
    evidence: str             # verbatim sentence from the report
    detail: str = ""


@dataclass
class SupplyMap:
    symbol: str
    document: str
    pages_read: int
    chars: int
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    customers: list = field(default_factory=list)
    facilities: list = field(default_factory=list)
    family: list = field(default_factory=list)
    locations: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("inputs", "outputs", "customers", "facilities", "family"):
            d[k] = [asdict(n) if isinstance(n, Node) else n for n in getattr(self, k)]
        return d


def _sentences(text: str) -> list[str]:
    """Split extracted PDF text into sentence-ish units.

    PDF extraction leaves hyphenation and hard line breaks; both are healed
    before splitting or half the sentences arrive truncated."""
    t = re.sub(r"-\n(\w)", r"\1", text)
    t = re.sub(r"\s+", " ", t)
    parts = re.split(r"(?<=[.;])\s+(?=[A-Z(])", t)
    return [p.strip() for p in parts if 40 <= len(p.strip()) <= 400
            and not _NOISE.match(p.strip())]


def _find_terms(sentence: str) -> list[str]:
    low = sentence.lower()
    return sorted({c for c in _COMMODITY
                   if re.search(rf"\b{re.escape(c)}\b", low)}, key=len, reverse=True)


# Running heads, folios and cover furniture survive PDF extraction and read
# like sentences. They must never win the "best evidence" contest.
_FURNITURE = re.compile(
    r"\|\s*[A-Z]|Integrated Annual Report|Annual Report 20|"
    r"^\(?[A-Z]\s+in (crores|lakhs)|Corporate Overview|Statutory Report|"
    r"Financial Statement", re.I)


def _evidence_score(sentence: str, term: str, pat: re.Pattern) -> float:
    """How good a quote is this? Proximity of the marker to the commodity
    beats digit-count, which used to hand the prize to page headers."""
    if _FURNITURE.search(sentence):
        return -100.0
    low = sentence.lower()
    ti = low.find(term.lower())
    m = pat.search(sentence)
    if ti < 0 or m is None:
        return -50.0
    proximity = abs(m.start() - ti)
    score = 12.0 - proximity / 40.0
    score += min(len(re.findall(r"\d", sentence)), 6) * 0.5
    # mid-length sentences read best as evidence
    n = len(sentence)
    score -= abs(n - 170) / 90.0
    return score


def _collect(sents: list[str], markers, kind: str, limit: int = 8) -> list[Node]:
    """Sentences matching any marker, keyed by the commodity they name."""
    pat = re.compile("|".join(markers), re.I)
    best: dict[str, tuple[float, str]] = {}
    counts: Counter = Counter()
    for s in sents:
        if not pat.search(s):
            continue
        terms = _find_terms(s)
        if not terms:
            continue
        for t in terms[:2]:
            counts[t] += 1
            score = _evidence_score(s, t, pat)
            if t not in best or score > best[t][0]:
                best[t] = (score, s)
    out = []
    for t, _ in counts.most_common(limit * 2):
        sc, ev = best.get(t, (-100.0, ""))
        if sc <= -50.0:          # nothing quotable survived: drop the node
            continue
        out.append(Node(term=t, kind=kind, mentions=counts[t], evidence=ev))
        if len(out) >= limit:
            break
    return out


def _facilities(sents: list[str]) -> list[Node]:
    out: list[Node] = []
    seen = set()
    for s in sents:
        if not re.search(r"capacit|manufacturing unit|plants?\b|distiller|refinery", s, re.I):
            continue
        caps = _CAPACITY.findall(s)
        if not caps:
            continue
        key = " ".join(f"{a}{b}" for a, b in caps[:3])
        if key in seen or _FURNITURE.search(s):
            continue
        seen.add(key)
        out.append(Node(term=", ".join(f"{a} {b}" for a, b in caps[:4]),
                        kind="facility", mentions=1, evidence=s))
        if len(out) >= 6:
            break
    return out


def _family(sents: list[str], self_name: str = "") -> list[Node]:
    pat = re.compile("|".join(_FAMILY_MARKERS), re.I)
    # Proper-noun runs ending in a company suffix are the candidates.
    name_pat = re.compile(
        r"\b((?:[A-Z][A-Za-z&.\-]*\s+){1,5}"
        r"(?:Limited|Ltd\.?|Private Limited|Pvt\.? Ltd\.?|LLP|Inc\.?|BV|B\.V\.))")
    found: dict[str, str] = {}
    negatives: list[str] = []
    for s in sents:
        if not pat.search(s):
            continue
        if re.search(r"(?:does\s*n[o']t|no)\s+(?:have\s+any\s+)?subsidiar", s, re.I):
            negatives.append(s)
            continue
        for m in name_pat.findall(s):
            nm = m.strip()
            # A company is not its own subsidiary; its name appears on every
            # other page and would otherwise top the family list.
            if self_name and self_name.lower()[:14] in nm.lower():
                continue
            if len(nm) > 6 and nm not in found and not _FURNITURE.search(nm):
                found[nm] = s
    nodes = [Node(term=k, kind="family", mentions=1, evidence=v)
             for k, v in list(found.items())[:12]]
    return nodes, negatives


def build_supply_map(symbol: str, text: str, document: str,
                     pages: int, company_name: str = "") -> SupplyMap:
    """Assemble the map. Everything returned quotes the document."""
    sents = _sentences(text)
    fam, negatives = _family(sents, company_name)
    locs = [s for s in _STATES if re.search(rf"\b{re.escape(s)}\b", text)]
    m = SupplyMap(
        symbol=symbol.upper(), document=document, pages_read=pages,
        chars=len(text),
        inputs=_collect(sents, _INPUT_MARKERS, "input"),
        customers=_collect(sents, _CUSTOMER_MARKERS, "customer"),
        facilities=_facilities(sents),
        family=fam,
        locations=locs[:12],
    )
    # Outputs: commodities named alongside selling/producing verbs.
    m.outputs = _collect(sents, (r"manufactur", r"produc(?:e|tion) of", r"we sell",
                                 r"sale of", r"our products", r"portfolio of"),
                         "output")
    if negatives:
        m.notes.append(negatives[0])
    m.notes.append(
        f"every node quotes the filed report ({pages} pages read); nothing is "
        "inferred - a counterparty the report does not name does not appear")
    if not m.inputs and not m.customers:
        m.notes.append("no input/customer language matched: the report may be "
                       "image-only (scanned), or use wording outside the "
                       "patterns - the document link is above, unaltered")
    return m
