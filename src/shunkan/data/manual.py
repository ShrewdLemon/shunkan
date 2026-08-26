"""Human/agent-in-the-loop extraction, using the same gate as the model path.

WHY THIS EXISTS. The DeepSeek budget ran out with 14 of the 59 core companies
unseeded. Reading a 400k-token filing end to end is not something an agent can
do at scale either, so this narrows the problem the way a human analyst
actually would: the supply-chain content in an Indian annual report is
concentrated in a handful of sections, and the rest of the document is
financial statements, notes, governance boilerplate and the notice of AGM.

Nothing here relaxes the evidence rule. Whatever is extracted goes through
validate_against_source exactly like a model answer, so a quote that is not in
the filing is dropped whoever wrote it. That symmetry is the point: the gate
does not care who is asserting.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from shunkan.data.provider import DataError

# Headings and phrases that reliably sit above supply-chain prose in Indian
# annual reports. Ordered by how much signal they carry, not alphabetically.
_SECTION_CUES = (
    r"plant locations?\b", r"manufacturing (?:facilit|location|unit|division)",
    r"our (?:products|brands|portfolio|business|operations|customers)",
    r"business (?:overview|model|segments?)\b",
    r"products? (?:and|&) services", r"product portfolio",
    r"raw material", r"procurement", r"sourcing", r"supply chain",
    r"key customers", r"customer segments?", r"clientele", r"end[- ]use",
    r"segment (?:information|revenue|report)", r"principal business",
    r"nature of business", r"value chain", r"capacity utili[sz]ation",
    r"installed capacity", r"exports?\b", r"distribution network",
    r"related party transactions?", r"subsidiar(?:y|ies)",
    r"management discussion", r"operational (?:review|performance|highlights)",
)
_CUE_RE = re.compile("|".join(_SECTION_CUES), re.I)

# Pages that are almost never worth reading for this purpose.
_SKIP_RE = re.compile(
    r"notice of the \d+|notice is hereby given|independent auditor'?s report"
    r"|balance sheet as at|statement of profit and loss|cash flow statement"
    r"|notes? to the (?:standalone|consolidated)? ?financial statements"
    r"|corporate governance report|secretarial audit|form no\. mgt"
    r"|remuneration of directors|attendance slip|proxy form", re.I)


def high_signal_pages(pages: list[str], budget_chars: int = 170_000) -> list[tuple[int, str]]:
    """Rank pages by supply-chain signal and return the best, in page order.

    Scored on cue density rather than raw length, because a page of segment
    tables is worth more than three pages of the chairman being optimistic.
    Pages that look like statutory boilerplate are excluded outright - they
    are long, they are numerous, and they say nothing about who buys what.
    """
    scored = []
    for i, p in enumerate(pages):
        if not p or len(p) < 400:
            continue
        if _SKIP_RE.search(p[:1500]):
            continue
        cues = len(_CUE_RE.findall(p))
        if not cues:
            continue
        # density, with a mild bonus for pages that name a proper noun run
        caps = len(re.findall(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+){1,3}\b", p))
        scored.append((cues * 1000 + caps, i, p))
    scored.sort(reverse=True)
    out, used = [], 0
    for _, i, p in scored:
        if used + len(p) > budget_chars:
            continue
        out.append((i, p))
        used += len(p)
    return sorted(out)


def prepare(symbol: str, budget_chars: int = 170_000) -> dict:
    """Fetch the newest readable filing and return its high-signal pages."""
    from shunkan.data.filings import latest_readable_report

    import pypdfium2 as pdfium  # noqa: F401  (import proves the dep is present)

    ar, text, pages_read = latest_readable_report(symbol)
    pages = text.split("\n")  # fetch_report_text joins pages with \n
    # re-split properly: fetch_report_text joins page texts with a newline, so
    # rebuild page boundaries by re-reading is overkill; chunk on form feeds
    # where present, else fall back to fixed windows.
    if text.count("\f") > 5:
        pages = text.split("\f")
    else:
        step = max(1, len(text) // max(pages_read, 1))
        pages = [text[i:i + step] for i in range(0, len(text), step)]
    sel = high_signal_pages(pages, budget_chars)
    return {"symbol": symbol.upper(), "report": ar, "pages_read": pages_read,
            "chars": len(text), "selected": sel,
            "selected_chars": sum(len(p) for _, p in sel), "text": text}


def commit(symbol: str, payload: dict, *, text: str, report: dict,
           pages: int, author: str = "claude-opus-5") -> dict:
    """Put a hand-built payload through the SAME gate as a model answer."""
    from shunkan.data.llm import (CATEGORIES, Extraction, store_extraction,
                                  validate_against_source)

    kept, dropped = validate_against_source(payload, text)
    ex = Extraction(
        symbol=symbol.upper(),
        document=f"AR FY{report.get('to_year')}",
        document_url=report.get("url", ""),
        pages=pages, chars=len(text),
        undisclosed=[str(u) for u in (payload.get("undisclosed") or [])],
        dropped=dropped, model=author, effort="targeted-sections",
        extracted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **kept)
    if dropped:
        ex.notes.append(f"{len(dropped)} node(s) dropped: neither the quote nor "
                        "the name occurs in the filed document")
    ex.notes.append(f"extracted by {author} from targeted sections of the filed "
                    f"report ({pages} pages); every node checked verbatim "
                    "against the full document text")
    store_extraction(ex)
    return {"symbol": ex.symbol, "counts": ex.counts(), "dropped": len(dropped),
            "match_mix": {m: sum(1 for c in CATEGORIES for x in getattr(ex, c)
                                 if x.get("match") == m)
                          for m in ("exact", "prefix", "recovered")}}


# ------------------------------------------------------------------ digest

_CUES = {
    "INPUTS": r"raw material|feedstock|procure|sourcing|sourced from|purchase[sd]? of|"
              r"input cost|consumption of|supplier|we buy|imported from",
    "OUTPUTS": r"our products|product portfolio|brands? (?:include|such as)|we manufacture|"
               r"manufactures?\b|produces?\b|range of|launched|offerings include|"
               r"portfolio (?:of|comprises)|segment revenue",
    "CUSTOMERS": r"customers include|key customers|sold to|supplied to|our customers|"
                 r"end[- ]use|offtake|distributors?\b|dealers?\b|exports? to|export markets|"
                 r"institutional buyers|B2B|clients include|serves\b",
    "FACILITIES": r"plant|factor(?:y|ies)|manufacturing (?:unit|facilit|division|site)|"
                  r"located at|located in|installed capacity|capacity of|refinery|mill\b|"
                  r"warehouse|depot|branch(?:es)?\b|store[s]?\b",
}
_BOILER = re.compile(r"recognised at|in accordance with ind as|auditor|pursuant to "
                     r"(?:section|regulation)|previous year figures|refer note", re.I)


# Abbreviations that end in a full stop and are NOT sentence ends. Without
# this, "Dr. Fixit" splits after "Dr." and the brand becomes unfindable: the
# phrase appears 40 times in Pidilite's filing and grepping it returned
# nothing, because every occurrence had been cut in half.
_ABBREV = {"dr", "mr", "mrs", "ms", "shri", "smt", "prof", "lt", "col", "capt",
           "hon", "st", "jr", "sr", "ltd", "pvt", "co", "inc", "corp", "no",
           "nos", "vol", "fig", "rs", "approx", "etc", "viz", "vs", "i.e",
           "e.g", "u.s", "u.k"}
_CAND = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_LASTWORD = re.compile(r"([A-Za-z.]+)\.$")


def _split_sentences(flat: str) -> list[str]:
    """Split on sentence ends, but not on an abbreviation's full stop.

    Python cannot express this as a variable-width lookbehind, so: split on
    every candidate boundary, then glue back the ones that followed a known
    abbreviation.
    """
    parts = _CAND.split(flat)
    out: list[str] = []
    for part in parts:
        if out:
            m = _LASTWORD.search(out[-1].rstrip())
            if m and m.group(1).rstrip(".").lower() in _ABBREV:
                out[-1] = out[-1] + " " + part
                continue
        out.append(part)
    return out


def digest(text: str, per_category: int = 200) -> dict[str, list[str]]:
    """Reduce a filing to the sentences that plausibly carry supply-chain facts.

    345,000 tokens becomes about 9,000, which is the difference between a
    company you cannot read and one you can.

    THIS IS A RECALL AID, NOT A CLASSIFIER. The grouping is a hint from a
    regex and it is wrong often: half of CUSTOMERS will be board-member
    biographies because "serves" matched. The reader decides. Treating a
    bucket as the answer is precisely the mistake the old keyword extractor
    made when it promoted the word "gold" out of an award title into a
    Reliance product.
    """
    flat = re.sub(r"\s+", " ", text)
    sents = _split_sentences(flat)
    out, seen = {}, set()
    for cat, pat in _CUES.items():
        rx = re.compile(pat, re.I)
        keep = []
        for s in sents:
            s = s.strip()
            if not (60 <= len(s) <= 420) or not rx.search(s) or _BOILER.search(s):
                continue
            k = s[:70].lower()
            if k in seen:
                continue
            seen.add(k)
            keep.append(s)
            if len(keep) >= per_category:
                break
        out[cat] = keep
    return out


def digest_text(text: str, per_category: int = 200) -> str:
    d = digest(text, per_category)
    return "\n\n".join(f"########## {c} ({len(v)}) ##########\n"
                        + "\n".join(f"- {x}" for x in v) for c, v in d.items())


CATEGORIES_M = ("inputs", "outputs", "customers", "facilities")

# ------------------------------------------------- byte-exact quote building

def flatten(text: str) -> str:
    """Whitespace-collapsed document, the form quotes are sliced from."""
    return re.sub(r"\s+", " ", text)


def sentence_at(flat: str, marker: str, back: int = 0, fwd: int = 0) -> str | None:
    """The sentence in `flat` containing `marker`, sliced byte-exact.

    An extraction agent working through NIFTY 100 found the better method and
    it belongs here: do not RETYPE a quote, SLICE it. A retyped quote can
    silently lose a character - these filings are full of soft hyphens, the
    U+FFFE that pypdfium2 emits at line breaks, and non-breaking spaces - and
    the gate then rejects a true node for a transcription error nobody can
    see. Slicing makes the citation byte-exact by construction.

    Falls back to a bounded window when no sentence boundary is nearby, which
    happens constantly in bullet lists like "Major Orders Won" - the sections
    that name the most counterparties and that the digest surfaces worst.

    Pass `back`/`fwd` to force a window instead: three NTPC quotes came back
    as `prefix` rather than `exact` because a 200-character window straddled a
    page-break running head, and trimming the window to the bullet fixed all
    three.
    """
    lo = flat.find(marker)
    if lo < 0:
        return None
    if back or fwd:
        return flat[max(0, lo - back):lo + len(marker) + fwd].strip()
    start = flat.rfind(". ", 0, lo)
    start = 0 if start < 0 else start + 2
    end = flat.find(". ", lo + len(marker))
    end = len(flat) if end < 0 else end + 1
    out = flat[start:end].strip()
    if len(out) > 600:
        out = flat[max(0, lo - 200):lo + len(marker) + 200].strip()
    return out or None


def build_payload(text: str, spec: dict, undisclosed=()) -> tuple[dict, list]:
    """Turn {category: [(name, marker[, location][, (back, fwd)])]} into a payload.

    Returns (payload, problems). A marker that is not in the document is
    reported rather than silently dropped, because a typo in a marker and a
    fact that is not in the filing look identical in the output and are very
    different mistakes.
    """
    flat = flatten(text)
    payload: dict = {c: [] for c in CATEGORIES_M}
    payload["undisclosed"] = list(undisclosed)
    problems = []
    for cat, items in spec.items():
        for it in items:
            name, marker = it[0], it[1]
            loc, back, fwd = None, 0, 0
            for extra in it[2:]:
                if isinstance(extra, str):
                    loc = extra
                elif isinstance(extra, (list, tuple)):
                    back, fwd = extra
            q = sentence_at(flat, marker, back, fwd)
            if q is None:
                problems.append({"category": cat, "name": name,
                                 "problem": f"marker not found in document: {marker[:70]!r}"})
                continue
            row = {"name": name, "quote": q}
            if loc:
                row["location"] = loc
            payload[cat].append(row)
    return payload, problems

