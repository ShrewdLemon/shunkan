# India macro

The rate the whole book is priced off, and the decade behind it.

## Why it exists

Shunkan knew a great deal about 500 Indian companies and nothing at all about
the economy they trade in. For a derivatives terminal that is backwards: after
the underlying, the largest input to an option price is the risk-free rate,
and the largest driver of that rate is the MPC's next decision. A desk that
can price a straddle but cannot say what the repo rate is has a hole in it.

This was the one genuine **data** gap found while benchmarking against Koyfin.
Koyfin carries India macro and no India companies; Shunkan was the exact
inverse. Closing it makes the India picture complete on both axes.

## Two sources, deliberately unblended

| | RBI | World Bank |
|---|---|---|
| what | the policy corridor | long-run annual series |
| freshness | current | annual, ends in the prior year |
| authority | **is** the number | a compilation |
| use | trade against it | context only |
| access | scraped from the homepage | free JSON API, no key |

They are never merged into one strip. A 2025 annual CPI print sitting beside a
live repo rate, in the same visual register, invites reading the first as
today's — so the corridor is drawn large and current, and the series are drawn
quieter and stamped with the year they end.

## The corridor

Seven rates: policy repo, SDF (floor), MSF (ceiling), bank rate, fixed reverse
repo, CRR, SLR. Drawn as a band, because the *structure* is the information —
whether policy sits mid-corridor and how much room the MPC has before it hits
its own floor. A row of seven numbers says the same thing and shows none of it.

### Scraping discipline

A rate scraper's failure mode is not an exception. It is **a plausible-looking
stale number**, and RBI may restyle its homepage whenever it likes. So:

* every rate carries the label it was found under (`found_as`);
* a percentage must appear **within 90 characters** of its label — an
  unbounded search happily pairs "Policy Repo Rate" with a number from a press
  release three screens away, which is how a scraper starts reporting a rate
  that was true in 2019;
* a miss is a refusal that names itself, per rate;
* if **all seven** miss, `policy_rates()` raises rather than returning an empty
  corridor as fact;
* **there is no fallback value anywhere**, and a test asserts none appears.

### Flattening

Tags become a **space**, not a pipe and not nothing.

This started as a pipe on the reasoning that a delimiter stops a label welding
to the next cell's number. Tested against the real page, all three separators
read the same seven rates — so that reasoning demonstrated nothing, and the
choice was settled on the cases where they differ. Two are realistic and the
pipe loses both:

```
"Policy <b>Repo</b> Rate"      pipe -> "Policy |Repo| Rate"   no match
"<td>5.25</td><td>%</td>"      pipe -> "5.25|%"               no match
```

A space rejoins a label split by inline markup, still lets the number regex
span a cell boundary, and keeps distinct cells apart.

## The series

Six curated World Bank indicators — CPI, real GDP growth, FX reserves, current
account, unemployment, gross capital formation. Curated because a picker over
1,400 indicators is a research project, not a screen.

Fetched **concurrently**: six sequential HTTP calls with a retry each is a
40-second page load for data that changes once a year. They share nothing.

### A refusal is never cached

`series()` returns failures as a value rather than raising, so the plain
`@ttl_cache` that used to wrap it stored them like any other result — and one
transient timeout blanked the macro screen for **six hours**. Found exactly
that way: a blip during development, then a second call returning the same
failure in 0.0 s.

Only successful series are cached. Failures retry on the next call, and a
single blip is absorbed by one retry before it is reported at all.

Nulls are dropped, never zeroed: the World Bank sends `null` for years it has
no reading, and a null is not a 0 % inflation print.

## API

```
GET /api/macro          # corridor + series, one call
GET /api/macro/rates    # just the corridor — for the widget
```

`dashboard()` isolates the two blocks: RBI being unreachable says nothing
about the World Bank, and must not take its series down.

## Screens

* **MAC** — Analyse → Context, or type `MAC`.
* **RBI POLICY CORRIDOR** — a workspace widget, refreshed hourly rather than
  per tick. The MPC meets six times a year; polling a scraped page every 15
  seconds would be rude to RBI and useless to the reader.

## Tests

`tests/test_macro.py` — 17 cases. The parser is tested against page shapes RBI
actually serves, and each guard was verified by reintroducing the bug it
prevents and confirming the suite goes red.

> **Bytecode hazard, learned here.** Verifying a guard by patch → test →
> restore can defeat Python's `(mtime, size)` bytecode invalidation when the
> substitution is the same length and the cycle completes inside one second.
> `"|"` → `" "` is exactly that. Purge `__pycache__` between the patched and
> restored runs, or the "CAUGHT" is meaningless.
