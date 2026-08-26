# Playbook: seeding the company database by hand

How an agent extracts a company's supply chain from its annual report without
an LLM API, and why each step is the way it is. Written 2026-08-26 while
seeding NIFTY 100, for whoever picks this up next.

Read `EXTRACTION.md` first — it covers the pipeline, the validation gate and
the traps in the source data. This document is only about the *manual* path:
what to do when there is no API budget, or when a company deserves better
evidence than a model produces.

## The problem this solves

An Indian annual report is **300–600 pages, roughly 345,000 tokens**. You
cannot read fifty of those. But the supply-chain content — what the company
buys, makes, sells and where — occupies maybe 3% of the document. The rest is
financial statements, notes, governance boilerplate, the notice of AGM and
several hundred pages of the chairman being optimistic.

So: narrow first, read second. The digest step below turns 345k tokens into
about 9k, which is the difference between "impossible" and "twenty minutes".

## The loop

```
prepare(sym)  ->  digest()   ->  READ  ->  payload.json  ->  commit()
   ~30s            ~2s          ~9k tok      you            gate + store
```

### 1. `prepare` — fetch the newest READABLE filing

```python
from shunkan.data.manual import prepare
d = prepare("ITC")        # -> {report, text, pages_read, chars, selected}
```

Uses `filings.latest_readable_report`, which matters more than it sounds:
NSE serves truncated bodies for some symbols and **duplicates the newest row
for exactly those symbols**, so a naive `annual_reports(sym)[0]` fails and
`[1]` retries the identical bad URL. `prepare` returns the report dict it
actually read, and you **must** label the extraction with that year. Reading
FY2025 and calling it FY2026 is worse than the download error it replaces.

Batch this. Downloads are I/O-bound and parallelise fine (the PDF *parse* is
serialised internally by `_PDF_LOCK` — PDFium is not thread-safe and fails as
though the input were corrupt, which will send you hunting the wrong bug).

### 2. `digest()` — reduce 345k tokens to ~9k

Scans the full text for sentences matching supply-chain cue patterns, dedupes
them, drops accounting boilerplate, and groups them under INPUTS / OUTPUTS /
CUSTOMERS / FACILITIES.

It is a **recall aid, not a classifier**. The grouping is a hint from a
regex; half the sentences under CUSTOMERS will be board-member biographies
because "serves" matched. That is fine and expected — you are reading, and you
decide. Never treat a digest bucket as the answer.

Cap around 200 sentences per category. Higher adds noise faster than signal.

### 3. Read it and write a payload

```json
{"inputs":[{"name":"...","quote":"..."}],
 "outputs":[...], "customers":[...],
 "facilities":[{"name":"...","location":"...","quote":"..."}],
 "undisclosed":["inputs"]}
```

### 4. `commit` — through the same gate as the model

```python
from shunkan.data.manual import commit
commit("ITC", payload, text=text, report=meta["report"], pages=meta["pages_read"])
```

This runs `validate_against_source` exactly as a model answer would be run.
**The gate does not care who is asserting.** Your quote gets checked against
the full document, and a node you invented is dropped the same as one the
model invented. That symmetry is deliberate — it is the only reason a
hand-built row can sit in the same table as a machine-built one.

## Rules for writing the payload

These are learned, not stylistic. Each one cost a rejected node.

**Name the node as the document names it.** The gate requires a citation to
share 60% of the name's content words. `"Fabelle chocolates"` against a quote
saying only *"an exclusive Fabelle store in Forum Mall, Bengaluru"* is 1 of 2
words and gets rejected. `"Fabelle"` passes. Every descriptor you add is a
word the evidence has to carry. This is the rule doing its job: if the filing
never calls it a chocolate, you are the one asserting that.

**One node, one claim.** `"KCC, SHG financing, Agri Gold Loans"` is three
products in a trenchcoat. Split them, each with the sentence that supports it.

**A customer is a buyer, not a channel.** Asian Paints' filing says it serves
customers *"through a multi channel distribution network comprising
wholesalers, distributors, retail outlets"* — wholesalers are the **channel**.
The customers are the homeowners and projects on the other side of the
sentence. The model got this wrong; do not repeat it.

**An input is bought, not produced.** Balrampur's molasses and bagasse come
out of its own crushing operation. They are intermediates, not purchases.
Titan's gold *is* an input — a jeweller buys gold. Ask "did money leave the
company for this?"

**An award is not a product.** The failure that started all of this: the old
keyword extractor listed **GOLD** as a Reliance product because the filing
said *"IGMC Gold (IRIM)"*, a prize. Certifications, rankings and awards
mention products without being products.

**A number is not a name.** `"50+ dosage forms and 1,500+ products"`,
`"1 in 3 Indian households"`, `"6,000th branch"` — these are statistics.

**Plans are not facts.** *"is contemplating to set up a greenfield alumina
refinery"* does not make bauxite an input today. If you keep it, the name must
say it is proposed.

**Copy the quote verbatim.** Do not tidy punctuation, expand an abbreviation
or join two sentences. The gate normalises whitespace and strips running
heads, and it tolerates an 80-character prefix when a sentence crosses a page
break — but it will not tolerate a sentence you improved.

**Use `undisclosed` and mean it.** Reliance names no suppliers at all; its
"Raw Material Security" section says only *"long-term supply arrangements and
strategic partnerships"*. Banks generally have no inputs in this sense. An
empty category with the reason named is a better answer than a filled one
that is guessed. Both models tested refused to do this, which is why the gate
exists — do not be the third.

## Reading a filing efficiently

Where the facts actually live, in rough order of yield:

| looking for | look in |
|---|---|
| plants, capacity, locations | the **Plant Locations** annexure near the shareholder-information pages; BRSR Principle 3 |
| named customers | related-party transactions; segment notes; export paragraphs |
| products and brands | the business-segment sections of the Directors' Report |
| raw materials | MD&A cost paragraphs; BRSR Principle 2; the risk section |
| subsidiaries | the subsidiary-performance section, not the share-capital table |

Two document-specific warnings:

**The richest page is often a diagram.** Balrampur's page 122 is an LCA
process flow mapping *Ethanol → Oil Refineries*, *Granular Potash → Fertilizer
Vendors*, *Liquid CO₂ / Dry Ice / Fusel Oil → Vendors*. Those relationships
appear nowhere else in 540 pages. Text extraction preserves the labels but
destroys the arrows, so the direction has to be reasoned from wording. If a
company's map looks thin, render the figure-heavy pages as images and look.

**Never take a number from report text.** These filings embed a font named
`ITF Rupee` that maps ₹ onto the codepoint `H`, so every parser reads
`H450 crores`. Financial figures come from XBRL, always.

## Quality bar

ITC, the first hand-built company: **47 nodes, 47 exact citations, 0 dropped**.
That is the standard. A hand extraction should be *better* than the model's,
because you can name nodes as the filing names them and you can tell a channel
from a customer.

Check your work with the same audit the model gets:

```python
from shunkan.data.llm import load_extraction
ex = load_extraction("ITC")
print(ex.counts(), len(ex.dropped))
[x["match"] for c in ("inputs","outputs","customers","facilities") for x in getattr(ex, c)]
```

`recovered` matches are legitimate but weaker — the code found the sentence,
not you. A hand extraction with many `recovered` nodes means you were
paraphrasing; go back and copy properly.

## What is genuinely not feasible

Reading is ~9k tokens per company plus judgment. **NIFTY 100 is a long but
real task. NIFTY 500 by hand is not** — 500 × 9k is 4.5M tokens of reading
before a single node is written, and the judgment does not batch.

For the long tail, use `extract_from_text` (whole document) and spend the
$0.08/company.

**Do not feed the digest to the model to save money — it was tried and
measured.** On RELIANCE the digest path cost $0.0105 against $0.1116 and
returned 32 nodes against 68, missing Samsung C&T Corporation and India Gas
Solutions as customers. The cause is not the sentence cap; raising it from 60
to 250 gives a byte-identical digest. Only 152 of 2,407 candidate sentences
match any cue pattern, so recall depends on whether a company's prose happens
to use that vocabulary.

The digest works for a **reader** because a reader compensates for the
regex's blind spots — that is how ITC reached 47 nodes with zero drops. A
model handed the same digest just inherits the blind spots. $40 for all of
NIFTY 500 is not a saving worth half the data.

`revalidate()` re-runs the gate for **zero tokens**, so improving the
validator never means re-buying data.

## Where things are

| what | where |
|---|---|
| manual helpers | `src/shunkan/data/manual.py` |
| the gate | `src/shunkan/data/llm.py` → `validate_against_source` |
| stored extractions | `~/.shunkan/store/extraction/<SYM>.json` |
| digest script | `src/shunkan/data/manual.py` → `digest()` / `digest_text()` |
| what the UI shows | `app.js` → `drawSupplyMap`, reads `/api/company/{sym}/extract` |

Stored extractions carry `model` (or the agent name), `effort`,
`document_url`, `pages` and `extracted_at`, so any node traces to the run that
produced it. Hand extractions record `effort: "targeted-sections"` and the
agent name in `model` — the provenance says a human-shaped process produced
it, which is exactly what a reader should know.
