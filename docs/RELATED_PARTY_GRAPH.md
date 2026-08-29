# The related-party graph

How Shunkan turns BSE's related-party XBRL into a graph you can walk, and the
specific ways that goes wrong.

Read `docs/EXTRACTION.md` first if you want the annual-report side. This
document is about the *other* source, and about the join between them.

---

## 1. Why a second source at all

The annual-report extraction gives you sentences: *"The Oil to Chemicals (O2C)
business portfolio encompasses transportation fuels, polymers and
elastomers…"*. That is the company describing itself. It is checked verbatim,
it is honest, and it carries **no numbers and no counterparties by name** in
most cases.

The related-party filing gives you the opposite: named counterparties, rupee
amounts, half-yearly, with the relationship stated by the filer. *Reliance
sells ₹617,086 Cr to Reliance International Limited across six periods.*

Neither replaces the other and **they must never be blended**. A screen where
"X sells to Y" reads the same whether it came from a filed number or a glossy
sentence has destroyed the only thing that made the number worth having.
Every block in the NET view names which source it came from.

| | annual report | BSE RPT XBRL |
|---|---|---|
| what it is | a sentence the company wrote | a number the company filed |
| coverage | 498 of NIFTY 500 | 498 of NIFTY 500 |
| counterparties named | rarely | always |
| rupee values | no | yes |
| cadence | yearly | half-yearly, **frozen at Sep 2024** |
| regulation | Companies Act disclosure | SEBI LODR Reg 23(9) |

> The RPT endpoint is **deprecated and frozen**. Six periods exist, Mar 2022 →
> Sep 2024, and no more arrive. Say so on screen; do not present a stale
> aggregate as current.

---

## 2. Getting the data

`shunkan.data.bse`:

```python
scrip_code("RELIANCE")      # 500325  — via BSE's scrip master
rpt_periods(500325)         # the six qtrid values
rpt_rows(500325, qtrid)     # the rows for one period
harvest_rpt(...)            # store everything under ~/.shunkan/store/bse/
ingest_rpt(500325, symbol="RELIANCE")   # push into the graph
```

Two undocumented gates:

* **Headers.** `Referer: https://www.bseindia.com/` *and* an `Accept` header.
  Without both you get a 403 that looks like rate limiting and is not.
* **`qtrid`.** The period key. It is not in any BSE documentation; it is
  discovered from `rpt_periods()`, which reads the dropdown the site itself
  populates.

### Symbols that legitimately have nothing

`scrip_code` raising is usually **not** a lookup failure:

* **BSE Ltd** cannot list on its own exchange.
* **CDSL** listed away from BSE because BSE promotes it.

Neither is in the 4,979-scrip master, so every BSE-sourced feed is correctly
empty for them. The error says this, because a bare *"no scrip code"* invites
someone to fix it by loosening the name match until it hits a different
company. `MCX` and `CAMS` *are* in the master and resolve normally.

---

## 3. The two decisions that carry all the risk

### 3.1 Direction — only explicit trades get one

52% of all filed rows carry the transaction type **"Any other transaction"**.
That says nothing about who supplied whom. Turning it into a trade edge would
manufacture a supply relationship out of a disclosure catch-all.

Only these carry direction:

```
sale of goods or services · sale of goods · sale of fixed assets      -> sells_to
purchase of goods or services · purchase of goods · purchase of fixed assets
                                                                      -> buys_from
```

Everything else still proves the parties are related, so it lands as a
**relationship edge and nothing more**.

### 3.2 Relationship — the head names the party

This is where the graph has been wrong twice, badly, and silently.

Filers write **`HEAD of QUALIFIER`**. The head names the party. The qualifier
names who they are related *through*, which is a different claim.

| filed text | means | naïve substring scan said |
|---|---|---|
| `Director of Subsidiary Company` | a **person** | subsidiary |
| `KMP of Subsidiary` | a **person** | subsidiary |
| `Relative of KMP/Director of subsidiary` | a **person** | subsidiary |
| `Related Party of Subsidiary` | a party related to a subsidiary | subsidiary |
| `Subsidiary of parent company` | a **sibling** | subsidiary (a child) |
| `Interested entity of KMP/Director of subsidiary` | an entity via a person | subsidiary |

The damage was not abstract. HDFC Bank's page was about to claim **528
subsidiaries** — it has roughly ten — with *Ameet P. Hariani HUF* and *Ms. Heta
Hariani Ray*, a director's family holdings, among them. Each carried a source,
which made it look checked.

`normalise_relationship` therefore:

1. normalises whitespace and collapses `" -"` → `"-"`
   (`Wholly -Owned Subsidiary` is filed 944 times);
2. splits on the first `" of "` into head and qualifier;
3. classifies **person roles off the head** before anything structural;
4. consults the qualifier only where it genuinely changes the relation —
   a subsidiary *of the parent* is a fellow subsidiary;
5. falls back to the whole string, then to `related_party_of`.

`"Employee"` splits both ways and the qualifier decides: *Employee of Max
Healthcare Institute Limited* is a person; *Employee Welfare Trust* and
*Employees' Gratuity Fund* are entities. Hence `employee_benefit_plan_of`.

**It reads the relationship text and never the counterparty name.** An audit
that flags people by honorific reports *Dr. Reddy's Laboratories SA*, *Shri
Kannan Departmental Store Limited* and *Shri Siddhi Avenues LLP* as people.
A test pins this boundary so nobody "improves" it by sniffing names.

Relations produced:

```
subsidiary_of · wholly_owned_subsidiary_of · fellow_subsidiary_of
subsidiary_of_ultimate_parent · holding_company_of · associate_of
joint_venture_with · significant_influence_over · promoter_group_of
group_entity_of · key_management_of · relative_of_kmp
kmp_interested_entity_of · employee_benefit_plan_of
related_party_of_subsidiary · related_party_of
```

---

## 4. Entity resolution

Everything above is worthless if *Reliance Retail Limited* from the RPT feed
lands on a different node than the one the annual-report extraction made.

* **`resolve(name, kind=...)`** — `kind` is a **constraint, not a hint**. The
  same name legitimately exists as several kinds: `RELIANCE` is a company, and
  also a `customer` node because another filer lists it as a buyer. An earlier
  version fell through to the unfiltered alias when the kind-filtered lookup
  missed, so `resolve("RELIANCE", kind="company")` returned
  `customer:RELIANCE` — and `link_legal_names` then wrote 513 aliases onto
  wrong nodes.
* **`_KIND_RANK` / `prefer="company"`** — ranks kinds when several match.
* **`link_legal_names()`** — the graph keys companies by ticker; every filing
  writes *"Reliance Industries Limited"*. BSE's scrip master carries both, so
  the join needs no guessing. 996 aliases, 2 unmatched.
* **`_node_for()`** — memoised per ingest run. `resolve()` is a UNION query
  called twice per row across 538k rows; without memoisation and an index on
  `node(name)`, a 28-second job does not finish in ten minutes.

---

## 5. Walking it

```python
g.trade_summary(nid, top=20)   # counterparties by rupee value, per direction
g.structure(nid, limit=600)    # who it IS related to — limit is PER RELATION
g.structure_counts(nid)        # TRUE totals, from COUNT(*)
g.trail(nid, hops=2, max_nodes=260)
```

`structure_counts` exists because counting the rows that came back conflates
*"this is all of them"* with *"this is as many as we asked for"*. HDFC Bank
files 525 entities under one relation; a caller that trusted `len()` would
print a truncated total as a complete one.

`trade_summary` aggregates across periods — **a counterparty in six half-years
is one relationship, not six** — and keeps the per-period split so a trend
needs no second query.

`trail` reports its node cap rather than applying it silently. A truncated
graph looks exactly like a complete one.

### HTTP

```
GET /api/entity/{symbol}          # or a node id: company:RELIANCE INTERNATIONAL
GET /api/entity/{symbol}/trail?hops=2&max_nodes=260
```

Both accept a **node id** as well as a ticker, because a counterparty usually
has no ticker — that is what makes the second hop possible.

---

## 6. The view (NET)

`renderNetwork` in `app.js`. Rules it follows, all learned the hard way:

* **One rupees-per-pixel scale for both directions, printed on the diagram.**
  Independently scaling each side is the lie this picture tells well: it draws
  a ₹8,603 Cr purchase at the same height as a ₹617,086 Cr sale.
* **Counterparties past the top 12 are summed into a labelled remainder band**,
  never dropped, so the picture still adds to the header total.
* **A gap is not a zero.** A counterparty with no filing in a period may
  simply not have transacted; a floor reading would invent a number.
* **Period totals are bars, not a line.** Six discrete filings; a line implies
  readings between them that nobody filed.
* **`netCr` scales precision to magnitude.** Fixed 0-decimal crore formatting
  turned HDFC Bank's entire related-party book — every figure real, the
  largest ₹2.65 Cr — into a column of zeroes and a header reading *"₹0 Cr"*.
  That is the insider-dealing *"0% → 0%"* bug again. Nothing non-zero may ever
  render as `0`; below ₹0.01 Cr it reads `<0.01`, which is a statement about
  the display.
* **People are counted apart from corporate structure**, so the number at the
  top of CORPORATE STRUCTURE cannot be read as a subsidiary count.
* **The force layout is seeded deterministically by index.** `Math.random()`
  would move the picture under the reader on every open for no gain.

---

## 7. Tests

| file | guards |
|---|---|
| `tests/test_rpt_relationships.py` | 58 cases taken verbatim from filed XBRL; a regression that no person phrase may resolve to a company relation; the name-sniffing boundary |
| `tests/net_render_test.js` | renders the shipped functions over six payload shapes — rich, sub-crore, gappy, one-sided, single-period, empty; asserts no non-zero value renders as `0` across 56 magnitudes |
| `tests/test_company_page_dom.py` | the one-scale rule, the formatter, the remainder band, the un-runnable EXTRACT button |

The functions are **lifted out of `app.js`, not re-typed**, so editing them
breaks the tests.

---

## 8. Rebuilding

```bash
python tools/reingest_rpt.py            # resumable; skips what is done
python tools/reingest_rpt.py --fresh    # full pass
```

**Purge before re-ingesting after a relation change.** `put_edges` upserts on
`(src, dst, rel)`, so changing the relation leaves the wrong edge in place
rather than replacing it:

```sql
DELETE FROM edge WHERE source LIKE 'BSE RPT%';
```

Stop the server first — it holds the DB, and the `DELETE` fails with
`database is locked` while it runs.
