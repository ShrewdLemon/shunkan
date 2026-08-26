# Extraction task spec (agents, and the template for the DeepSeek bulk run)

You are extracting a company's supply chain from its NSE-filed annual report.
Working dir: /Users/shrewdlemon/Projects/Shunkan. Use `.venv/bin/python`.

READ FIRST: `docs/EXTRACTION_PLAYBOOK.md`. It is short and every rule in it
cost a rejected node.

## Per company, exactly this loop

```python
import json, pathlib, warnings; warnings.filterwarnings("ignore")
from shunkan.data.manual import digest_text, commit
P = pathlib.Path("/private/tmp/claude-501/-Users-shrewdlemon-Projects-Shunkan/deaee33b-ad0b-46f9-a341-84d888097caa/scratchpad/prep")
text = (P/f"{SYM}_full.txt").read_text()          # already downloaded for you
meta = json.loads((P/f"{SYM}_meta.json").read_text())
open(f"/tmp/{SYM}_digest.txt","w").write(digest_text(text, 32))   # ~7k tokens
# READ that digest file, then:
r = commit(SYM, payload, text=text, report=meta["report"], pages=meta["pages_read"])
```

`commit` runs the SAME validation gate as the model path. A quote that is not
in the document is dropped whoever wrote it. Report what `commit` returns.

## Payload shape

```json
{"inputs":    [{"name":"...","quote":"<verbatim from the document>"}],
 "outputs":   [{"name":"...","quote":"..."}],
 "customers": [{"name":"...","quote":"..."}],
 "facilities":[{"name":"...","location":"...","quote":"..."}],
 "undisclosed":["inputs"]}
```

## The rules that decide whether a node survives

1. **Name it as the document names it.** The gate requires the quote to share
   60% of the name's content words, stem-matched. `"Fabelle chocolates"`
   against a quote saying only *Fabelle* is 1 of 2 words and is REJECTED.
   `"Fabelle"` passes. Every descriptor you add is a word the evidence must
   carry.
2. **Copy the quote verbatim.** Do not fix punctuation, expand abbreviations
   or join sentences. Paste exactly what the digest line says, including odd
   characters like `￾` — those are in the source and the gate normalises them.
3. **A customer is a BUYER, not a channel.** "sells through wholesalers and
   distributors" — the wholesalers are the channel.
4. **An input is BOUGHT, not produced.** A sugar mill's molasses is its own
   output. A jeweller's gold is an input. Ask: did money leave the company?
5. **An award is not a product.** The bug that started this project: "GOLD"
   became a Reliance product because a sentence said "IGMC Gold (IRIM)".
6. **Not names**: bare numbers ("1 in 3 households"), category labels
   ("raw materials"), plans ("is contemplating a refinery"), junk (", PLANT").
7. **One node, one claim.** Split "KCC, SHG financing, Agri Gold Loans".
8. **`undisclosed` is a real answer.** Banks have no meaningful raw-material
   inputs; many companies never name a supplier. An empty category with the
   reason stated beats a guessed one. Use it.

## Quality bar

Hand-built companies so far: ITC 47 nodes / 0 dropped, NESTLEIND 30 / 0,
BEL 29 / 1, HDFCBANK 31 / 2 — **99% exact citations**. Aim for 25-50 nodes
per company and near-zero drops. If `commit` reports drops, look at the
reason, fix the name or the quote, and re-commit — it is idempotent.

Do NOT pad. A focused 25-node map that is all true beats 60 nodes of filler.

## Report back

One line per company: symbol, counts dict, dropped count, match mix. Then a
short note on anything that surprised you (a company whose filing names no
suppliers, a broken PDF, a category that was genuinely empty).
