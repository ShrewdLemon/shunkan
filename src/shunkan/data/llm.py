"""LLM-assisted extraction from filed documents, with the refusal enforced in code.

WHY THIS EXISTS
The regex supply-chain extractor read the Balrampur FY2026 annual report and
classified ethanol as an INPUT. The report says, in plain English on page 108:

    the "output" price (ethanol) from the "input" cost (sugarcane FRP)

The text was never the problem. A pattern matcher cannot read that sentence,
and no better PDF parser makes it able to. This module hands the whole
document to a model that can, and then refuses to trust it.

WHAT THE 2026-08-25 BAKE-OFF SETTLED (numbers are measured, not estimated)

  RETRIEVAL IS THE BOTTLENECK, NOT THE PARSER. Chunking the report and
  retrieving the top 8 matches found 7 customers and 2 plants. Sending the
  whole 254k-token document in ONE call found 19 customers and 11 plants,
  including IFFCO, UPPCL, Indian Railways and the Lucknow Cantonment Board
  by name. So: no chunking, no embeddings, no vector store. The document
  goes in whole. This is also why there is no RAG code here to maintain.

  DOCLING LOSES AT FULL CONTEXT. Docling rebuilds tables well and beat flat
  text 82% to 38% when context was scarce. But it emits "<!-- image -->" for
  flow diagrams and DELETES every label inside them. Over the same 90 pages
  "Fertilizer Vendors" appears 0 times in Docling's output and once in
  pymupdf's. A capable model realigns a flattened table by itself; it cannot
  recover text the parser threw away. So pypdfium2 (BSD, 18x faster than pypdf, and it keeps those labels).

  REASONING EARNS ITS COST. At reasoning_effort="none" the model returned 9
  customers and polluted inputs with molasses, bagasse and fermentable sugars
  - things Balrampur PRODUCES, not buys. At "medium" it returned 13 customers
  (recovering the flow-diagram relationships) and 5 genuinely purchased
  inputs. "high" costs the same and adds nothing. The delta none->medium is
  $0.0085 per report, about $4/year across all of NIFTY 500. Not a reason to
  economise. The real cost is wall-clock: 12s vs 149s.

  "none" IS ALSO NOT REPRODUCIBLE. Three identical calls at temperature 0
  returned 7, 16 and 19 inputs. A graph that changes shape on re-run is not a
  dataset. Higher effort narrowed the spread as well as improving content.

THE RULE THIS MODULE ENFORCES
Every extracted node must carry a verbatim quote, and validate_against_source
DROPS any node whose quote does not actually occur in the document. This is
not decoration - both models tested failed to refuse when asked to. Told
explicitly that returning nothing was correct, DeepSeek still listed "Telecom
services" as a raw-material INPUT for Reliance, whose report names no
suppliers at all. The model will not police itself, so the code does.

The check also catches a subtler defect seen in testing: the model returned
all ten Balrampur sugar units with per-unit districts but attached the same
generic sentence to every one as its quote. The data was right and the
citation was wrong. Quote-attachment drift is indistinguishable from
fabrication once it is in the graph, so it is treated the same way.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from shunkan.config import APP_DIR, ensure_dirs
from shunkan.data.provider import DataError

SETTINGS_FILE = APP_DIR / "llm.json"
LEDGER_FILE = APP_DIR / "llm_ledger.jsonl"

# Providers are OpenAI-shaped. Only the base URL and the model list differ,
# so a new provider is a dict entry rather than a new client class.
PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "env": "DEEPSEEK_API_KEY",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro",
                   "deepseek-v4-flash-vision-exp"],
        # USD per 1M tokens, cache-miss input / output. Rates change; they are
        # settings rather than constants so a price move is an edit, not a
        # silently wrong cost column.
        "rate_in": 0.28,
        "rate_out": 0.42,
    },
}

EFFORTS = ["none", "low", "medium", "high"]


@dataclass
class LLMSettings:
    """Everything tunable, persisted to ~/.shunkan/llm.json. No secrets here."""

    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    effort: str = "medium"
    # 32000, not 16000. At 16000 a 254k-token report spent the ENTIRE budget
    # on reasoning tokens and returned an empty string with
    # finish_reason="length". The failure looks like a bad prompt and is
    # actually a ceiling. Do not lower this without re-reading that sentence.
    max_tokens: int = 32000
    temperature: float = 0.0
    timeout_s: float = 2400.0
    max_pages: int = 600
    rate_in: float = 0.28
    rate_out: float = 0.42
    enabled: bool = False

    def validate(self) -> list[str]:
        """Return human-readable problems. Empty list means usable."""
        bad = []
        if self.provider not in PROVIDERS:
            bad.append(f"unknown provider {self.provider!r}")
        if self.effort not in EFFORTS:
            bad.append(f"effort must be one of {EFFORTS}")
        if self.max_tokens < 8000:
            bad.append("max_tokens below 8000 will be consumed by reasoning "
                       "before any answer is emitted")
        if not 0 <= self.temperature <= 2:
            bad.append("temperature out of range")
        return bad


def load_settings() -> LLMSettings:
    if SETTINGS_FILE.exists():
        try:
            raw = json.loads(SETTINGS_FILE.read_text())
            known = {f for f in LLMSettings().__dataclass_fields__}
            return LLMSettings(**{k: v for k, v in raw.items() if k in known})
        except (json.JSONDecodeError, OSError, TypeError):
            pass  # a corrupt settings file falls back to defaults, never crashes
    return LLMSettings()


def save_settings(**fields) -> LLMSettings:
    ensure_dirs()
    cur = asdict(load_settings())
    for k, v in fields.items():
        if k in cur and v is not None:
            cur[k] = v
    s = LLMSettings(**cur)
    problems = s.validate()
    if problems:
        raise DataError("; ".join(problems))
    SETTINGS_FILE.write_text(json.dumps(cur, indent=2))
    try:
        SETTINGS_FILE.chmod(0o600)
    except OSError:
        pass
    return s


def api_key(provider: str = "deepseek") -> str:
    """Key from credentials.json, else the provider's env var.

    Stored through brokers.save_credentials, which writes 0600 via a temp file
    and rename - see the incident note in that module. The key is NEVER
    written to settings, logged, or returned by any endpoint.
    """
    from shunkan.data.brokers import load_credentials

    try:
        k = (load_credentials().get(provider) or {}).get("api_key") or ""
    except Exception:
        k = ""
    return k or os.environ.get(PROVIDERS.get(provider, {}).get("env", ""), "")


def set_api_key(key: str, provider: str = "deepseek") -> None:
    from shunkan.data.brokers import save_credentials

    save_credentials(provider, api_key=key.strip())


def key_fingerprint(provider: str = "deepseek") -> str:
    """A stable, non-reversible hint so the UI can show WHICH key is loaded
    without ever echoing it. Last four characters only, like a card."""
    k = api_key(provider)
    return f"…{k[-4:]}" if len(k) >= 8 else ("set" if k else "")


# ---------------------------------------------------------------- transport

def chat(messages: list[dict], settings: LLMSettings | None = None,
         model: str | None = None) -> dict:
    """One completion. Returns content plus the usage the ledger needs.

    Raises DataError on anything that is not a usable answer, INCLUDING an
    empty completion, because an empty string that reads as "nothing was
    disclosed" is the most dangerous possible failure here.
    """
    import httpx

    s = settings or load_settings()
    prov = PROVIDERS.get(s.provider)
    if not prov:
        raise DataError(f"unknown provider {s.provider!r}")
    key = api_key(s.provider)
    if not key:
        raise DataError(f"no API key for {s.provider} - set one in the ADMIN tab")

    body = {
        "model": model or s.model,
        "messages": messages,
        "temperature": s.temperature,
        "max_tokens": s.max_tokens,
    }
    if s.effort:
        body["reasoning_effort"] = s.effort

    t0 = time.time()
    try:
        r = httpx.post(f"{prov['base_url']}/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json=body, timeout=s.timeout_s)
    except Exception as exc:
        raise DataError(f"{s.provider} unreachable: {exc}") from exc
    if r.status_code != 200:
        # Never echo the body verbatim - some providers reflect the request,
        # and the request carries the key in a header some proxies log.
        raise DataError(f"{s.provider} HTTP {r.status_code}: "
                        f"{r.text[:200] if r.status_code != 401 else 'unauthorised - check the key'}")
    j = r.json()
    if "error" in j:
        raise DataError(f"{s.provider}: {str(j['error'])[:200]}")
    choice = j["choices"][0]
    content = choice["message"].get("content") or ""
    u = j.get("usage", {}) or {}
    reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
    if not content.strip():
        raise DataError(
            f"model returned an empty completion (finish={choice.get('finish_reason')}, "
            f"{reasoning:,} reasoning tokens of {s.max_tokens:,} allowed). "
            "Raise max_tokens - the budget was spent thinking.")
    return {
        "content": content,
        "model": body["model"],
        "effort": s.effort,
        "finish": choice.get("finish_reason"),
        "tokens_in": u.get("prompt_tokens", 0),
        "tokens_out": u.get("completion_tokens", 0),
        "tokens_reasoning": reasoning,
        "secs": round(time.time() - t0, 1),
        "cost_usd": round(u.get("prompt_tokens", 0) / 1e6 * s.rate_in
                          + u.get("completion_tokens", 0) / 1e6 * s.rate_out, 6),
    }


def test_connection(settings: LLMSettings | None = None) -> dict:
    """Cheap round-trip so the ADMIN tab can prove the key works before a
    26-minute job discovers it does not."""
    s = settings or load_settings()
    probe = LLMSettings(**{**asdict(s), "max_tokens": 2000, "effort": "none"})
    out = chat([{"role": "user", "content": "Reply with exactly: ok"}], probe)
    return {"ok": "ok" in out["content"].lower(), "model": out["model"],
            "secs": out["secs"], "tokens_in": out["tokens_in"],
            "tokens_out": out["tokens_out"], "cost_usd": out["cost_usd"],
            "key": key_fingerprint(s.provider)}


# ------------------------------------------------------------- the schema

SCHEMA = ('{"inputs":[{"name":"","quote":""}],'
          '"outputs":[{"name":"","quote":""}],'
          '"customers":[{"name":"","quote":""}],'
          '"facilities":[{"name":"","location":"","quote":""}],'
          '"undisclosed":["<category the report does NOT disclose>"]}')

PROMPT = (
    "You are building a supply-chain map from a company's annual report. "
    "The FULL report text follows.\n\n"
    "Extract:\n"
    "  inputs     - raw materials/feedstock the company BUYS or CONSUMES from outside.\n"
    "               NOT things it produces itself from its own process.\n"
    "  outputs    - products it MANUFACTURES and SELLS.\n"
    "  customers  - the NAMED counterparties or buyer classes that purchase from it.\n"
    "               A customer is a BUYER, never a commodity.\n"
    "  facilities - plants/units, with location where the text states one.\n\n"
    "HARD RULES:\n"
    " - Every item MUST carry a quote copied VERBATIM from the text below.\n"
    "   Copy the sentence that supports THAT item, not a nearby general one.\n"
    " - Use ONLY this document. No outside knowledge about the company.\n"
    " - If the report does not disclose a category, name that category in\n"
    "   'undisclosed' and leave its list empty. Returning nothing is CORRECT\n"
    "   when nothing is disclosed. Never invent a name to fill a slot.\n"
    " - Be exhaustive; the report is long.\n\n"
    f"Return STRICT JSON:\n{SCHEMA}\n\nFULL REPORT TEXT:\n")

CATEGORIES = ("inputs", "outputs", "customers", "facilities")

# ~4 chars/token, kept under a 300k-token window with room for the prompt and
# the reply. Balrampur's 1.48M chars came in at 408k prompt tokens and was
# accepted, so this is deliberately conservative rather than tuned to a limit
# the provider has not published.
_MAX_CHARS = 1_600_000


# Running heads, folios and the page furniture that a PDF text layer splices
# into the middle of a sentence that happens to straddle a page break.
_FURNITURE = re.compile(
    r"(integrated annual report[^|]*\|\s*\d+"
    r"|annual report \d{4}-\d{2}"
    r"|\|\s*\d{1,3}\s*\|"
    r"|page \d{1,3} of \d{1,3})", re.I)

# How much of a quote must match verbatim when the whole of it does not.
# 80 characters is far past anything a model produces by accident: a
# fabricated sentence does not share an 80-character exact prefix with a
# document it never read. Short enough, though, to survive one running head
# spliced into the tail of a sentence that crosses a page boundary.
_PREFIX_MIN = 80


def _norm(s: str) -> str:
    """Whitespace-insensitive form for quote matching.

    A PDF text layer breaks sentences at the line box, so the document holds
    'Ethanol sold to Oil\\nRefineries' while the model returns the sentence
    reflowed. Matching raw would reject almost every true quote, and a
    validator that rejects the truth gets switched off. Collapse instead.
    """
    return re.sub(r"\s+", " ", _FURNITURE.sub(" ", (s or ""))).strip().lower()


def _locate(quote: str, hay: str) -> str | None:
    """Return "exact", "prefix", or None.

    Measured on the Balrampur FY2026 report: a strict whole-quote match
    dropped six TRUE outputs - molasses, bagasse, pressmud, co-generated
    power, ethanol, agricultural fertilizers - every one of them supported by
    a real sentence. Their first 160 characters matched the document exactly
    and the tails did not, because the sentences cross a page break and the
    text layer splices the running head into them. A gate that discards real
    data is worse than no gate: it launders a false negative as diligence.
    So: whole quote first, then a long verbatim prefix.
    """
    nq = _norm(quote)
    if len(nq) < 12:
        return None
    if nq in hay:
        return "exact"
    if len(nq) >= _PREFIX_MIN and nq[:_PREFIX_MIN] in hay:
        return "prefix"
    return None


@dataclass
class Extraction:
    """One extraction, with everything needed to audit or reproduce it."""

    symbol: str
    document: str = ""
    document_url: str = ""
    pages: int = 0
    chars: int = 0
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    customers: list = field(default_factory=list)
    facilities: list = field(default_factory=list)
    undisclosed: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    model: str = ""
    effort: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_reasoning: int = 0
    cost_usd: float = 0.0
    secs: float = 0.0
    extracted_at: str = ""

    def counts(self) -> dict:
        return {c: len(getattr(self, c)) for c in CATEGORIES}


def validate_against_source(payload: dict, text: str) -> tuple[dict, list]:
    """THE GATE. Keep only nodes whose quote genuinely occurs in the document.

    Returns (kept, dropped). Every dropped node keeps its reason so the ADMIN
    tab can show what the model tried to assert and why it did not land - a
    silent drop would just be a quieter kind of unaccountability.
    """
    hay = _norm(text)
    kept: dict = {c: [] for c in CATEGORIES}
    dropped: list = []
    for cat in CATEGORIES:
        seen: set = set()
        for item in (payload.get(cat) or []):
            if not isinstance(item, dict):
                dropped.append({"category": cat, "name": str(item)[:80],
                                "reason": "not an object"})
                continue
            name = str(item.get("name") or "").strip()
            quote = str(item.get("quote") or "").strip()
            if not name:
                dropped.append({"category": cat, "name": "", "quote": quote[:120],
                                "reason": "no name"})
                continue
            if not quote:
                dropped.append({"category": cat, "name": name,
                                "reason": "no quote supplied"})
                continue
            how = _locate(quote, hay)
            if how is None:
                dropped.append({"category": cat, "name": name, "quote": quote[:400],
                                "reason": ("quote too short to verify"
                                           if len(_norm(quote)) < 12
                                           else "quote not found in the document")})
                continue
            key = _norm(name)
            if key in seen:
                continue  # the model repeats a counterparty under variant spellings
            seen.add(key)
            row = {"name": name, "quote": quote, "match": how}
            if item.get("location"):
                row["location"] = str(item["location"]).strip()
            kept[cat].append(row)
    return kept, dropped


def extract_from_text(symbol: str, text: str, *, document: str = "",
                      document_url: str = "", pages: int = 0,
                      settings: LLMSettings | None = None) -> Extraction:
    """Whole document -> one call -> validated nodes. No chunking anywhere."""
    s = settings or load_settings()
    # A 500-page report can exceed even a 300k-token window. Truncate and SAY
    # SO, rather than letting the provider reject the call (which reads as a
    # transport failure) or silently dropping the tail (which reads as "the
    # company disclosed nothing about its customers").
    note = None
    if len(text) > _MAX_CHARS:
        note = (f"document truncated to {_MAX_CHARS:,} of {len(text):,} characters "
                f"to fit the model context - the tail was NOT read")
        text = text[:_MAX_CHARS]
    out = chat([{"role": "user", "content": PROMPT + text}], s)
    m = re.search(r"\{.*\}", out["content"], re.S)
    if not m:
        raise DataError("model did not return JSON "
                        f"(finish={out['finish']}, {out['tokens_out']:,} output tokens)")
    try:
        payload = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise DataError(f"model returned malformed JSON: {exc}") from exc

    kept, dropped = validate_against_source(payload, text)
    ex = Extraction(
        symbol=symbol.upper(), document=document, document_url=document_url,
        pages=pages, chars=len(text),
        undisclosed=[str(u) for u in (payload.get("undisclosed") or [])],
        dropped=dropped, model=out["model"], effort=out["effort"],
        tokens_in=out["tokens_in"], tokens_out=out["tokens_out"],
        tokens_reasoning=out["tokens_reasoning"], cost_usd=out["cost_usd"],
        secs=out["secs"],
        extracted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **kept)

    if note:
        ex.notes.append(note)
    if dropped:
        ex.notes.append(f"{len(dropped)} node(s) dropped: their quote does not "
                        "occur in the filed document")
    for cat in CATEGORIES:
        if not getattr(ex, cat):
            ex.notes.append(f"no {cat} survived validation - the report may not "
                            f"disclose them (Reliance, for one, names no suppliers)")
    ex.notes.append(f"every node quotes the filed report ({pages} pages, "
                    f"{len(text):,} chars) and was checked against it verbatim")
    ledger_append(ex)
    return ex


def extract_company(symbol: str, *, settings: LLMSettings | None = None,
                    progress=None) -> Extraction:
    """Locate the latest annual report, read it, extract, validate, store."""
    from shunkan.data.filings import annual_reports, fetch_report_text

    s = settings or load_settings()
    if not s.enabled:
        raise DataError("LLM extraction is disabled - enable it in the ADMIN tab")
    sym = symbol.upper()

    def say(m):
        if progress:
            progress(m)

    say("locating the annual report")
    ars = annual_reports(sym)
    ar = ars[0]
    say(f"downloading FY{ar.get('to_year')} report ({ar.get('size')})")
    text, pages = fetch_report_text(ar["url"], max_pages=s.max_pages)
    if len(text) < 5000:
        raise DataError(
            f"annual report yielded only {len(text):,} characters from {pages} "
            "pages - it is probably image-only (scanned). Not extracting from it: "
            "a near-empty document produces confident nonsense.")
    say(f"extracting from {pages} pages ({len(text)//4:,} tokens) "
        f"via {s.model} @ effort={s.effort}")
    ex = extract_from_text(sym, text, document=f"AR FY{ar.get('to_year')}",
                           document_url=ar["url"], pages=pages, settings=s)
    store_extraction(ex)
    return ex


# ------------------------------------------------------------- persistence

def store_dir():
    from shunkan.store.store import STORE_DIR

    d = STORE_DIR / "extraction"
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_extraction(ex: Extraction) -> None:
    """One JSON per symbol, plus graph edges.

    One file per symbol rather than a shared parquet, for the reason the news
    archive learned the hard way: two writers doing read-modify-write on one
    file lose rows. Extractions are written by a background job and read by
    the web process, so they never share a file.
    """
    path = store_dir() / f"{ex.symbol}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(ex), indent=1))
    os.replace(tmp, path)
    try:
        _push_graph(ex)
    except Exception:
        pass  # the graph is a projection; never lose an extraction over it


def load_extraction(symbol: str) -> Extraction | None:
    path = store_dir() / f"{symbol.upper()}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        known = {f for f in Extraction("").__dataclass_fields__}
        return Extraction(**{k: v for k, v in raw.items() if k in known})
    except Exception:
        return None


def stored_symbols() -> list[str]:
    return sorted(p.stem for p in store_dir().glob("*.json"))


_REL = {"inputs": "consumes", "outputs": "produces",
        "customers": "sells_to", "facilities": "operates"}


def _push_graph(ex: Extraction) -> None:
    """Project the extraction onto the knowledge graph.

    Every edge carries the document URL as its source, because put_edges
    raises ValueError without one - the graph refuses unsourced claims by
    construction, and this module has nothing to hide from it.
    """
    from shunkan.store.graph import GraphStore, normalise

    g = GraphStore()
    nodes, edges = [{"id": ex.symbol, "kind": "company", "name": ex.symbol}], []
    for cat, rel in _REL.items():
        for item in getattr(ex, cat):
            nid = f"{cat[:3]}:{normalise(item['name'])}"
            nodes.append({"id": nid, "kind": cat[:-1], "name": item["name"]})
            edges.append({"src": ex.symbol, "dst": nid, "rel": rel,
                          "as_of": ex.extracted_at[:10],
                          "source": ex.document_url or ex.document,
                          "meta": json.dumps({"quote": item["quote"][:400],
                                              "model": ex.model})})
    g.put_nodes(nodes)
    g.put_edges(edges)


# ------------------------------------------------------------------ ledger

def ledger_append(ex: Extraction) -> None:
    """Append-only spend and provenance log.

    Every call is recorded whether or not its nodes survived validation,
    because the cost was incurred either way and a ledger that only lists
    successes understates what the pipeline actually costs.
    """
    ensure_dirs()
    row = {"ts": ex.extracted_at, "symbol": ex.symbol, "model": ex.model,
           "effort": ex.effort, "tokens_in": ex.tokens_in,
           "tokens_out": ex.tokens_out, "tokens_reasoning": ex.tokens_reasoning,
           "cost_usd": ex.cost_usd, "secs": ex.secs, "pages": ex.pages,
           "kept": sum(ex.counts().values()), "dropped": len(ex.dropped)}
    with open(LEDGER_FILE, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    try:
        LEDGER_FILE.chmod(0o600)
    except OSError:
        pass


def ledger(limit: int = 200) -> list[dict]:
    if not LEDGER_FILE.exists():
        return []
    rows = []
    for line in LEDGER_FILE.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:][::-1]


def ledger_stats() -> dict:
    rows = ledger(limit=100000)
    if not rows:
        return {"calls": 0, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0,
                "symbols": 0, "kept": 0, "dropped": 0}
    return {
        "calls": len(rows),
        "cost_usd": round(sum(r.get("cost_usd", 0) for r in rows), 4),
        "tokens_in": sum(r.get("tokens_in", 0) for r in rows),
        "tokens_out": sum(r.get("tokens_out", 0) for r in rows),
        "tokens_reasoning": sum(r.get("tokens_reasoning", 0) for r in rows),
        "symbols": len({r.get("symbol") for r in rows}),
        "kept": sum(r.get("kept", 0) for r in rows),
        "dropped": sum(r.get("dropped", 0) for r in rows),
        "secs": round(sum(r.get("secs", 0) for r in rows), 1),
        "last": rows[0].get("ts", ""),
    }


# ------------------------------------------------------------- bulk seeding

def bulk_extract(symbols: list[str], *, workers: int = 5,
                 budget_usd: float | None = None, skip_existing: bool = True,
                 settings: LLMSettings | None = None, progress=None) -> dict:
    """Seed the database across a universe, concurrently and within a budget.

    THE BUDGET GUARD IS NOT OPTIONAL. Each company is a ~300k-token call, and
    a universe of 500 is real money. The guard is checked BEFORE each call is
    dispatched, so the run stops cleanly rather than discovering the account
    is empty halfway through and leaving a half-seeded database that looks
    complete. What was skipped is returned, never silently dropped.

    Concurrency is modest on purpose. These are minutes-long calls against a
    third-party API; twenty parallel requests is how you get rate-limited into
    a partial seed with no record of which companies failed.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    s = settings or load_settings()
    if not s.enabled:
        raise DataError("LLM extraction is disabled - enable it in the ADMIN tab")

    have = set(stored_symbols()) if skip_existing else set()
    todo = [x.upper() for x in symbols if x.upper() not in have]
    spent = 0.0
    done, failed, skipped_budget = [], [], []
    lock = __import__("threading").Lock()

    def say(m):
        if progress:
            progress(m)

    say(f"bulk: {len(todo)} to extract, {len(have)} already stored, "
        f"budget {'$%.2f' % budget_usd if budget_usd else 'none'}")

    def one(sym):
        nonlocal spent
        with lock:
            if budget_usd is not None and spent >= budget_usd:
                skipped_budget.append(sym)
                return None
        try:
            ex = extract_company(sym, settings=s)
            with lock:
                spent += ex.cost_usd
                done.append(sym)
                say(f"  [{len(done)}/{len(todo)}] {sym}: {ex.counts()} "
                    f"dropped={len(ex.dropped)} ${ex.cost_usd:.4f} "
                    f"(spent ${spent:.2f})")
            return ex
        except Exception as exc:
            with lock:
                failed.append({"symbol": sym, "error": str(exc)[:200]})
                say(f"  [{len(done)}/{len(todo)}] {sym}: FAILED {str(exc)[:90]}")
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex_:
        futs = [ex_.submit(one, x) for x in todo]
        for _ in as_completed(futs):
            pass

    return {"requested": len(symbols), "extracted": done, "failed": failed,
            "skipped_already_had": sorted(have & {x.upper() for x in symbols}),
            "skipped_over_budget": skipped_budget,
            "spent_usd": round(spent, 4)}
