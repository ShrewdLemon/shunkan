# Document extraction — how it works and why it is built this way

Turning a 540-page NSE-filed annual report into a supply-chain map: what the
company buys, what it makes, who buys it, and where its plants are.

Every design choice below was measured on 2026-08-25 against the Balrampur
Chini Mills and Reliance Industries FY2026 reports. The numbers are recorded
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

### Matching is deliberately whitespace- and furniture-tolerant

A strict whole-string match is wrong, and we know because it was tried: it
dropped six **true** outputs (ethanol, co-generated power, agricultural
fertilizers, and others) whose sentences cross a page boundary, where the text
layer splices the running head into the middle of the sentence.

So matching is layered:

1. Normalise — collapse whitespace, strip running heads and folios.
2. Whole quote present → `match: "exact"`.
3. Else first **80 characters** present verbatim → `match: "prefix"`.
4. Else drop, keeping the claim and the reason.

80 characters is far past accident. A model does not share an 80-character
exact prefix with a document it never read, but a real quote survives one
spliced page header. Prefix-verified nodes are labelled as such in the UI.

**Dropped nodes are shown, never hidden.** A silent drop is a quieter kind of
unaccountability, and the drop list is also the fastest way to see a prompt
regression.

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
