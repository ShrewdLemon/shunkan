# Document extraction — how it works and why it is built this way

Turning a 540-page NSE-filed annual report into a supply-chain map: what the
company buys, what it makes, who buys it, and where its plants are.

Every design choice below was measured, on 2026-08-25/26, against the
Balrampur, Reliance, AXISBANK and ASIANPAINT FY2026 filings and a 59-company
seed of NIFTY 50 + BANKNIFTY. The numbers are recorded
here so nobody has to re-derive them, and so a future change has something to
argue against.

## The pipeline

```
NSE /api/annual-reports  ->  pypdfium2  ->  whole document, one LLM call
                                                    |
                                          validation gate (quote must
                                          occur in the source)
                                                    |
                              ~/.shunkan/store/extraction/<SYM>.json
                                        + knowledge-graph edges
                                        + ~/.shunkan/llm_ledger.jsonl
```

No chunking. No embeddings. No vector store. No RAG. That is a deliberate
result, not an omission — see below.

## Why each piece

### pypdfium2, not pypdf and not pymupdf

Measured on Balrampur FY2026 (400 pages, 16.6 MB):

| parser | time | chars | diagram labels | licence |
|---|---|---|---|---|
| pypdf (what we shipped before) | 7.2s | 1,057,520 | — | BSD |
| pymupdf | 0.9s | 1,059,889 | preserved | **AGPL** |
| **pypdfium2** | **0.4s** | **1,080,236** | preserved | **BSD/Apache** |

pypdfium2 is fastest, extracts the most text, and carries no copyleft. The
last column decided it: Shunkan is MIT and published on PyPI, so an AGPL
dependency would encumber every downstream user.

### No RAG — retrieval was the bottleneck

Same document, same model, same prompt; only the retrieval strategy differs:

| approach | customers found | plants found |
|---|---|---|
| chunk + embed + top-8 retrieval | 7 | 2 |
| **whole document in one call** | **19** | **11** |

Full context named IFFCO, UPPCL, Indian Railways and the Lucknow Cantonment
Board. Top-k retrieval structurally cannot find a fact mentioned once on page
217 of 540 — and for an evidence map, the single-mention facts are the
valuable ones. RAG earns its place when you are answering unpredictable
questions over a large corpus; this is exhaustive extraction from one known
document, which is a different problem.

### Docling was tested and rejected

Docling is genuinely good and it wins when context is scarce — on a
90-page slice with top-8 retrieval it beat flat text 82% to 38%, because it
rebuilds tables that flat extraction shatters into word columns.

At full context it **loses**, because it emits `<!-- image -->` for a flow
diagram and discards every label inside it. Over the same 90 pages:

| diagram label | Docling | pypdfium2 |
|---|---|---|
| `Fertilizer Vendors` | 0 | 1 |
| `Dry Ice sold` | 0 | 1 |
| `Fusel Oil` | 0 | 1 |

That matters because the densest supply-chain content in an Indian annual
report is often an infographic. Balrampur's page 122 LCA diagram maps
*Ethanol → Oil Refineries*, *Granular Potash → Fertilizer Vendors*,
*Liquid CO₂ / Dry Ice / Fusel Oil → Vendors* and appears nowhere else in the
document. A capable model realigns a flattened table by itself; it cannot
recover text the parser deleted.

Docling also costs 26 min/report and a 1.3 GB install (torch 536 MB). Keep it
in mind only if numeric table extraction is ever needed — and it is MIT, so it
stays available.

### `reasoning_effort: "medium"`

| effort | out tokens | secs | inputs | customers | $/report |
|---|---|---|---|---|---|
| none | 2,429 | 12 | 10 (polluted) | 9 | $0.0722 |
| low | 9,874 | 62 | 6 | 8 | $0.0754 |
| **medium** | 22,476 | 149 | 5 clean | **13** | $0.0807 |
| high | 22,752 | 149 | 4 clean | 13 | $0.0808 |

Reasoning buys two things. Customers rise 9 → 13, and only medium and above
recover the flow-diagram relationships. Inputs get *smaller and more correct*:
`none` listed molasses, bagasse and fermentable sugars, which Balrampur
**produces itself** rather than buys; higher effort excluded them.

`none` is also not reproducible — three identical calls at `temperature: 0`
returned **7, 16 and 19** inputs. A graph that changes shape on re-run is not
a dataset.

The cost delta is $0.0085/report, about $4/year across all of NIFTY 500. The
real cost is wall-clock: 12s vs 149s. `high` costs the same as medium and adds
nothing.

### `max_tokens: 32000`

At 16,000 a 254k-token report spent its **entire** completion budget on
reasoning tokens and returned an empty string with `finish_reason="length"`.
The failure presents as a bad prompt and is actually a ceiling. `chat()`
raises a `DataError` naming the reasoning-token count when this happens, so it
can never again be mistaken for "the document disclosed nothing".

## The validation gate

`validate_against_source()` keeps only nodes whose quote actually occurs in
the document, and it exists because **the model will not refuse on its own.**

Told explicitly that returning nothing was correct, and given an `undisclosed`
field to use, DeepSeek still listed *"Telecom services"* as a raw-material
input for Reliance — whose report names no suppliers at all. qwen3:14b failed
the same way, returning placeholders that echoed the query text back.

It also catches outright citation fabrication. Real case from the first run:

- **Document:** *"Revenue from sale of sugar and its by-products is recognised at a point in time…"*
- **Model returned:** *"Revenue from sale of sugar and its by-products **(such as molasses, bagasse and pressmud)** is recognised at a point in time…"*

The model **inserted the parenthetical** to manufacture support for three
nodes it wanted to create. The nodes are arguably true; the citation was
invented. In an evidence-first system those are the same failure.

### The three tiers

A node survives only if the filing supports it. Three tiers, tried in order:

| tier | meaning | when |
|---|---|---|
| `exact` | the model's quote appears verbatim in the filing | best case |
| `prefix` | its first 80 characters appear verbatim | the sentence crosses a page break and the running head is spliced in |
| `recovered` | the quote did not verify, but the ENTITY is named — so the code lifts the document's own sentence | the model paraphrased its citation |

Anything that clears none of these is dropped, with the claim and the reason
kept and shown on the page.

### It also checks the quote is about the right thing

A citation must share **60% of the node name's content words**, stem-matched.
This closes a hole found by auditing 809 seeded nodes: the gate verified a
quote *occurred* in the filing and never that it *mentioned* what it was cited
for, so generic prose sailed through as `exact`:

- **BAJAJ-AUTO** — *"…wholly owned subsidiary in Thailand with an issued and
  subscribed share capital of THB 45 million"* cited for an **Engineering
  Design Centre**. It establishes a subsidiary, never a building.
- **AXISBANK** — *"These services are offered to all customers (related /
  unrelated) in the ordinary course of business"* cited for **both** LIC
  Housing Finance **and** IDBI Bank. It names neither.
- **BALRAMCHIN** — `"Others 3066.55 566.93"` cited as a raw material.

56 of 809 nodes shared under a third of the name's content words; 11 shared
none. Proportion rather than presence, because the Thailand sentence shares
*bajaj/auto/thailand* and misses every word that makes it a design centre.
Stems rather than exact tokens, because `Gold Loans` must still survive *"gold
loan portfolio"* — the target is boilerplate, not paraphrase.

### Why recovery exists

On AXISBANK the strict gate rejected 18 of 66 nodes. Checked by hand the
**entities** were real — *Kisan Credit Card*, *neo by Axis Bank*, *Axis House*
all appear in the filing — while the sentences the model wrote around them did
not. **The model finds what a company sells reliably and reproduces the prose
unreliably.** Dropping those throws away true facts over a bad citation;
keeping the model's sentence publishes a fabricated quote. So the gate does
neither: the model is the finder, the code stays the witness. AXISBANK went
48 kept/18 dropped → 59 kept/7 dropped.

### revalidate() costs nothing

The model's answer is stored verbatim, so improving the validator does not
mean re-buying every extraction. `revalidate(symbol)` re-reads the filing,
re-runs the current gate, and returns before/after so a validator change can
be **judged rather than assumed** — the first version of this gate silently
discarded six true Balrampur outputs and nothing in the pipeline showed it.

## NSE serves truncated filings

The `PDFium: Data format error` failures were never a parser problem. **NSE's
archive serves incomplete PDF bodies with a `Content-Length` that matches the
short body**, so httpx sees a clean 200 and raises nothing. Not HTML, not a
redirect stub, not encrypted, and not a pypdfium2 defect — pymupdf fails
identically on the same bytes.

**Persistent (3 of 59).** AUBANK's FY2026 object declares `/L 22,660,544` and
the API says 21.61 MB; the edge serves 130,516 or 424,177 bytes, and a `Range`
request confirms the *origin* believes it is 424 KB. COALINDIA is 56.8% short,
NTPC 17.5%. Unfixable client-side.

**Transient (any symbol, any run).** NSE's edge replicas disagree with each
other. AUBANK FY2025 pulled eight times returned a complete 21,318,690 bytes
four times and a truncated 18,677,760 four times — `Content-Length` agreeing
with the body every time. NTPC returned three *different* truncation lengths
across three runs.

Completeness is tested on the **`%%EOF` trailer**, not `/L` vs
`Content-Length`: KOTAKBANK's `/L` is 20 bytes under its length and it parses
fine at 522 pages, and APOLLOHOSP and PNB carry no `/L` at all. `%%EOF`
flagged all three real cases with no false positives.

`latest_readable_report()` walks **distinct** URLs newest-first — distinct
because NSE returns a duplicated newest row for exactly the symbols whose
newest upload is broken, so a naive `ars[1]` retries the identical bad URL. It
returns the *report*, not just the text, because the extraction must be
labelled with the year actually read. **Reading FY2025 and calling it FY2026
would be worse than the download error it replaces.** Filings older than about
FY2024 arrive as ZIPs holding one PDF; those are unwrapped so deep fallback
does not dead-end.

## PDFium is not thread-safe

And it fails as though the input were bad. Five parallel extractions produced
`Data format error` for BAJAJ-AUTO, BAJAJFINSV and BAJFINANCE, and 4,494
characters from ASIANPAINT's 294 pages. All four parse perfectly alone —
ASIANPAINT gives 64,387 chars in its first 20 pages. Taken at face value those
look like corrupt or scanned filings, and the honest-refusal path would have
recorded them as such and seeded a permanent hole. `fetch_report_text` holds a
module lock and closes the handle inside it; parsing is 0.4s against a ~170s
model call, so callers keep their concurrency.

## What the keyword extractor did, for the record

It listed **GOLD as a product of Reliance**, because `"gold"` was in its
commodity list and the sentence read *"…IGMC **Gold** (IRIM), and IMeXI India
Icon Kaizen Award."* That is an award. It also emitted `, PLANT` as a facility
and put a share-capital table under CUSTOMERS. Every quote was real; every
inference from it was wrong, and a keyword matcher cannot tell the difference.

An audit of 809 LLM-extracted nodes found **no** node anywhere lifted from an
award title. The eight surviving "Gold" nodes — `Gold Loans`, `Agri Gold`,
`Digital Gold Loan`, `WoodTech PU Gold` — are all real products.

## What is stored, and where

| path | contents | notes |
|---|---|---|
| `~/.shunkan/store/extraction/<SYM>.json` | full extraction + dropped nodes + provenance | one file per symbol; written atomically via temp+rename |
| `~/.shunkan/llm_ledger.jsonl` | one line per API call | append-only; 0600 |
| `~/.shunkan/llm.json` | settings | 0600. **never contains the key** |
| `~/.shunkan/credentials.json` | API key under `deepseek.api_key` | 0600, temp+rename |
| `~/.shunkan/shunkan.db` | graph edges | every edge carries the report URL as its source |

One file per symbol rather than a shared parquet, for the reason the news
archive learned expensively: two writers doing read-modify-write against one
file lose rows.

Each stored extraction carries `model`, `effort`, `tokens_in`, `tokens_out`,
`tokens_reasoning`, `cost_usd`, `secs`, `document_url` and `extracted_at`, so
any node can be traced to the exact call that produced it.

Graph edges use relations `consumes`, `produces`, `sells_to`, `operates`, and
carry the quote in edge metadata. `put_edges` raises `ValueError` without a
source, so the graph refuses unsourced claims by construction.

## The ADMIN tab

`ANL → ADM`, or `/#admin`. Model, reasoning effort, max tokens, temperature,
page cap, price rates, the API key, and an enable switch. Every control shows
the measured number that justifies its default.

The key field is **write-only**: the server returns a fingerprint (`…4e8a`)
and there is no endpoint that returns the key. Leaving the field blank keeps
whatever is stored.

`TEST CONNECTION` does a two-token round-trip so a bad key fails in a second
rather than after a 16 MB download.

Extraction is off by default (`enabled: false`) — it spends money and calls a
third party, so it should be a decision.

## Cost in production

$0.0807 per company at medium effort. NIFTY 500, refreshed annually: **~$40/year**.
The whole bake-off that produced this document cost **$0.93**.

## Known ceilings

**Suppliers are usually not disclosed.** Reliance's "Raw Material Security"
section says only *"long-term supply arrangements and strategic
partnerships"*. No parser and no model extracts what was never filed. This is
the real gap to Bloomberg SPLC, which sources supplier arrows from customs and
shipping records rather than filings. Single-commodity names like Balrampur
disclose their value chain in detail; diversified groups deliberately do not.

**Numbers must never come from report text.** These reports embed a font
literally named `ITF Rupee` that maps ₹ onto the codepoint `H`, so every
parser — pypdf, pymupdf and Docling alike — reads `H450 crores`. Financial
figures come from XBRL, always.

**Vision is right for diagrams and not yet usable.**
`deepseek-v4-flash-vision-exp` read the page-122 diagram and correctly
identified *Molasses from Kumbhi Plant* as an input — something no text
ordering establishes — then hallucinated *"Rubber Ash"* where the page says
*Potash Ash*, and dropped every output→customer box. Revisit when a stronger
vision model ships; the modality is correct, the model is not.

## Reproducing the study

Artifacts and the full bake-off report:
<https://claude.ai/code/artifact/1e633bc4-b671-453c-81bf-6a4e4ce1fa7c>

Tests covering the gate, including the real fabrication case, are in
`tests/test_llm_extraction.py`.
