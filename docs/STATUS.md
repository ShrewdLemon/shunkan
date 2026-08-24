# Status

Kept honest on purpose. If something here reads as more finished than it is,
that's a bug in this file.

Last updated: 2026-08-13.

## Solid

Things with real test coverage that have been verified against a live broker.

- **Option chain (OPT).** IST expiry clock, correct lot sizes from the
  instruments dump, no synthetic fallback on the live path, keyed timers,
  in place repaint, expiry selector, ATM window, real previous-session ΔOI
  basis. Verified live on NIFTY.
- **Position book.** Multi venue instrument identity, side per lot ledger,
  sell to open, short side cash, FIFO offsetting. 13 tests in `test_book.py`.
- **Exchange margin.** Kite basket SPAN via `POST /margins/basket`. Verified
  live: naked short call 1,76,265, short straddle 2,00,583, iron condor 71,387.
- **Net book Greeks.** Delta, gamma, theta, vega, rho netted across positions,
  unmarkable legs named and excluded. Verified live on a 2 lot short straddle.
- **Instruments archive.** Daily contract master per venue, idempotent.
- **Backtest validators.** A permutation test on block-shuffled positions and a
  deflated Sharpe (Bailey & Lopez de Prado). The gate requires BOTH, because
  each has a blind spot the other covers: permutation cannot see selection
  bias, deflation cannot see whether the timing does anything. A best-of-800
  search over pure noise now fails. The trial count is read off the search
  itself (`trials_of`), so it cannot be understated by a caller; an unknown
  search object raises rather than defaulting to 1. `POST /api/backtest` with
  `mode: "validate"` runs the gate.
- **Self-contained broker setup.** Both first-run credentials and the daily
  re-auth happen from the broker chip in the top bar. The CLI still works but
  is no longer required for anything.
- **Feed state.** Liveness is observed rather than asserted: LIVE requires a
  tick inside the last two minutes, and the badge degrades to STALE on its own
  when they stop. Verified across all five states in a browser.

## Works but thinly tested

- **SABR calibration.** Now fitted to live chains. BANKNIFTY at 12 DTE fits
  TIGHT (RMSE 0.205 vol points, rho -0.098). NIFTY at 5 DTE fits POOR at every
  beta from 0.0 to 1.0, which looks like a real property of a short-dated smile
  rather than a defect; the tool says POOR rather than pretending. Solving from
  bid/ask mid instead of last price was what made the difference.

- **Order ticket.** Verified in a browser against live Kite quotes on
  2026-08-13: click a premium, defaults to SELL, digits set lots, S/B flip
  side, Enter books, Esc and click-away both dismiss. Booked a real 1-lot
  short and closed it back to flat.
- **Margin.** Verified live: a 1-lot NIFTY short priced at SPAN 1,48,416 +
  exposure 31,851 against a premium credit, from Zerodha's own calculator.
- **Margin trigger.** `POST /api/portfolio/margin`, fired unprompted by PRT once
  per book state and forceable with REPRICE. `price_margin()` is idempotent on
  the book fingerprint, so a desk pays one exchange call per adjustment. Unit
  tested against a stubbed basket call; the live round trip is still the one
  verified by hand, not by a test.
- **Settling an expired position.** Verified in a browser on 2026-08-17:
  SETTLE opens a ticket, the preview arithmetic is exact (short 65 @ 42.50
  settled at 0 previewed and recorded realized 2,762.50), Enter records, the
  book goes flat, and the journal row carries the settlement flag.
- **Screener auto refresh.** Verified in a browser: query runs, 20 rows, the
  keyed 5-minute timer registers, header says AUTO 5m.
- **MCX, BFO, CDS.** All venues exercised against the live API. BFO verified
  earlier (SPAN 169,620 for 1 lot short SENSEX CE). MCX/CDS rupee math went
  live 2026-08-18: economic multipliers sourced per contract
  (`shunkan/data/contract_specs.py`), each verified against Kite's SPAN
  calculator (GOLD 1 lot = 9.28% of the 1.55 crore contract value; the
  big/mini contract pairs agree to basis points, which is the multiplier
  proof). The book stores economic units everywhere; the Kite boundary
  converts back to lots for order-in-lots venues.

## Known rough edges

- **Two inline `onclick="show(...)"` sites remain** (tape snapshot, screener).
  They interpolate an exchange ticker from our own API into a JS string inside
  an HTML attribute. They work and the input is not user content, but they are
  the same shape as the pulse row bug and should move to delegation when those
  views are next touched.
- **The router covers view + symbol only.** Per-view modifiers (chart period
  and interval, chain expiry, payoff strategy, quant tab) are not in the URL,
  so a deep link restores the screen and the instrument but not the exact
  configuration. That is where the breakage risk concentrated and it is worth
  roughly a fifth of the value.
- **Browser verification needs a HARD reload.** A hash-only navigation never
  re-fetches, so bumping `?v=` is not enough on its own: two checks in a row
  passed against a cached `app.js` that did not contain the change under
  test. Always reload ignoring cache before believing a browser result.

- **Anything binding 127.0.0.1 inside a container is unreachable from the
  host.** Docker forwards a published port to the container's eth0, never to
  its loopback. This bit the OAuth catcher: the Kite login succeeded, the
  redirect reached the container's network stack, and nothing was listening on
  the interface it arrived on. `_catcher_bind()` now returns 0.0.0.0 in a
  container and 127.0.0.1 on a host. If you add another listener, remember it.

- **A container can report healthy while being unreachable.** The healthcheck
  runs inside the container, so it passes whenever the app is up, regardless of
  whether the port was ever published. `docker run` and Docker Desktop's Run
  button both expose without publishing. Docker gives the container no way to
  see its own host mapping, so the startup log warns instead.

- **Live delta P&L is first order only.** Net delta times the spot move since
  the last full mark. Gamma, theta and vega are all moving too, so it is not
  the book's P&L and is labelled as what it is. It resets on every mark rather
  than accumulating drift, and blanks when the feed is not LIVE.

- **Data age is only wired on two panels.** The chain and the book carry a real
  source timestamp and show AS OF plus an age that goes amber at 30s and red at
  2 minutes. Every other panel still says FETCHED, which is honest but weaker.
  Ticks are receipt-timed, not exchange-timed: the ticker subscribes in quote
  mode (44 bytes), which carries no exchange timestamp. Full mode (184) does,
  at triple the bandwidth.
- **Settlement blotter.** PRT now renders the journal: last 30 entries with a
  SETTLED tag on asserted closes, under the positions table.
- **Cold load.** Measured 20.6s (and 32s on a cold Yahoo path). The last real
  pulse snapshot is now persisted and served in ~25ms, painted with an honest
  AS OF age while the live fetch runs. Offline mode neither writes nor serves
  it, so a synthetic board can never masquerade as a snapshot.
- **No accounts.** By design for a single user localhost tool. `shunkan serve`
  now refuses a non-loopback bind unless you pass `--i-understand-the-risk`,
  and when you do it mints a per-run token that every `/api/` call and the tick
  socket require. That is a guard rail, not a security model: it is one shared
  secret and there is still no notion of users.

## Daily analysis and event studies

- **BRF is the Daily Analysis**: root to derivatives - chart facts, VIX
  percentile against 2008+, live chain positioning, NSE participant-wise
  FII/DII/Client/Pro nets with day-over-day change, event base rates with
  baselines, news bias. Facts only, no verdict by design. On 2026-08-17 the
  participants table independently matched Sensibull's published read for the
  same session (FII bearish, Client bullish) from the same NSE source file.
- **Participant archive**: 254 sessions backfilled from NSE's public archive,
  kept current by a 6-hourly loop. Backfillable, so no survivorship problem.
- **Event engine** (`analytics/events.py`): shocks standardised by lagged
  trailing vol, non-overlapping forward windows, and a baseline beside every
  conditional stat. Company studies can be run in excess-of-index terms at a
  stated beta of 1. The derivatives layer of event studies still needs the
  capture archive to accumulate.

## Store concurrency, the incident and the rule

- **What happened (2026-08-17):** the host news backfill and the container's
  news loop both did read-modify-write on one parquet. When the loop's read
  landed mid-write it saw an unreadable file, and the recovery path -
  "rewrite rather than lose the fetch" - kept its own rows and clobbered the
  archive from 10,084 rows to 675. The recovery path caused the loss. All of
  it was refetchable, which is luck of the channel, not design.
- **The rule now:** one writer per file. News writes per-origin files
  (live / rotation / backfill) and readers merge and dedup; there is nothing
  to lock across a macOS host and a Linux container, so file discipline
  replaces locking. And no fallback anywhere may overwrite what it cannot
  read: unreadable files are quarantined to *.corrupt.parquet with their
  bytes intact. Applied to news, harvest, and participant stores; each has a
  test that fails on the old behaviour.
- **Still exposed:** ChainStore.snapshot is single-file read-modify-write and
  safe only because the container is its sole writer. Host research scripts
  must treat the store as read-only while the container runs; anything that
  calls get_chain from the host writes through _capture and races the 60s
  loop. Live-verified MCX/BFO/CDS this session; found lot_size=1 on MCX/CDS
  (Kite's order-in-lots convention, not the economic multiplier). UNBLOCKED
  2026-08-18: multipliers sourced and SPAN-verified for 14 MCX + 4 CDS
  contracts — see contract_specs.py, which carries the verification numbers
  with the table. NICKEL is 250 kg now, not the stale 1500. Names outside
  the table still refuse with the reason spelled out.

- **First settlements recorded 2026-08-18 15:39 IST.** All 210 contracts of
  the expiring NIFTY weekly harvested in the window between the close and
  Kite's overnight delete; 1,259 contracts carry the day's session candle
  after the full sweep. Spot-check: BANKNIFTY26AUG46000CE settled 11,442 =
  index close minus strike, intrinsic to the rupee. Combined with the day's
  200+ chain snapshots (solved IVs + mids, 60s) and futures bars, this is
  the first complete expiry-day record — the 0-DTE decay study becomes
  testable as these accumulate.

## News archive

- **Headlines persist now.** `store/news/headlines.parquet`, fed by a 30-minute
  server loop (market feed + a rotating slice of constituent-name queries) and
  a per-company weekly-window backfill against Google News RSS, which honours
  after:/before: (verified before building). Mapping is by company name IN THE
  TITLE, longest-alias-first, against the NIFTY50 and BANKNIFTY constituent
  lists fetched from NSE's own archive. The collision traps (Kotak Mahindra
  Bank vs M&M vs Tech Mahindra, L&T vs LT) each have a test.
- **The backfill channel is a sample, not a census.** A retrospective query
  returns what Google indexes today, ranked by Google, ~100 items per window.
  Rows carry origin=backfill so research can always separate the channels, and
  old timestamps are date-granular, so daily joins only.
- **Verified end to end:** one year of RELIANCE = 1,289 unique rows, 389
  title-tagged; all 16 of its ±2σ days inside the window join to named news
  within ±1 day. Presence-of-news is now measurable; separating informative
  news from ambient coverage is the research this enables, not solves.

## Tick bus (WS06, third piece)

`/ws/ticks` is no longer a firehose. `shunkan/stream/bus.py` routes ticks
per client: the watchlist is auto-subscribed on connect, and each view can
`{"op":"sub","symbols":[...]}` its own symbol — opening a chart on something
outside the watchlist starts its ticks, leaving the view stops them.
Refcounts drive the exchange socket (Kite allows mid-session subscribe), so
the feed streams exactly the union of what somebody is looking at. Slow
clients get bounded queues with drop-oldest and a COUNTED drop figure in
`/api/status` under `ticks` — backpressure loses frames visibly, never
silently. Unknown symbols come back named in the ack, not swallowed.

One race caught during real-socket verification that in-process tests
missed: the hello used to be written straight to the socket while the drain
task pumped the queue, and over a real network a tick outran the hello.
Everything now rides the one queue; the drain task is the socket's only
writer. Verified 3/3 ordered over the wire and end-to-end in the browser
(WIPRO subscribed by opening its chart, prints on the tape, prints stop on
leaving the view while the watchlist streams on).

## Capture bug caught 2026-08-18: chains stored zero IVs

Found while adding data-age stamps: every captured chain snapshot had 0/25
strikes with an IV. The chain was built with `call_iv = NaN, solved from
prices on demand` — and no demand ever came from the snapshot path, so the
20-day local IV-rank promise could never fill, silently. IVs are now solved
AT FETCH (`kite_fno._solve_chain_ivs`) from the same mid-else-LTP price
`quote_price()` uses, so the UI, SABR and the archive see identical numbers,
and snapshots store the mids so the IVs stay re-derivable. First real
`atm_iv_history` observation landed 2026-08-18 (0.1695): day 1 of 20.
Old snapshots keep their NaN IVs — re-solving history from stored LTPs would
mix stale-LTP solutions into a mid-solved series, which is how the SABR
smile got poisoned once already. Related: one 2026-08-10 snapshot with an
honestly-recorded NaN spot was crashing every reader that walked the
history (all-NA idxmin); readers now skip such days by design.

## Front-future tape (2026-08-18 afternoon)

The watchlist feed now streams the front NIFTY/BANKNIFTY futures beside the
cash names. Their tape carries the volume the index never prints, which is
what makes VWAP and any day-structure read real: the daily analysis reports
the front future's VWAP with a note saying whose number it is. The feed
self-heals: a startup race once built a feed without its futures and nothing
said so - the keepalive loop now repairs every cycle and reports the outcome
in /api/status under feed_keepalive.futures. Bus routing resolves symbols
the feed already streams by identity, so NIFTYFUT is subscribable even
though no cash resolver knows it.

## The knowledge graph, and why it is not a graph database (2026-08-24)

Opening a company used to refetch everything - profile, XBRL filings, a
400-page annual report - every single time. There is now one SQLite file
(`~/.shunkan/shunkan.db`) holding a typed graph plus a content cache, and
the difference is measurable: a company page went from **5.8s cold to
0.029s after a container restart**, because the cache is on disk rather
than in a process that dies.

**Why SQLite and not Neo4j/Kuzu.** Every relationship the terminal knows -
ownership, fund holdings, index calls, sectors, beneficial owners -
totals ~77k edges today and ~120k at full NSE scale. Measured on that
data: alias resolve 0.4ms, one-hop neighbours 27ms, a three-hop control
chain (person -> promoter LLPs -> companies) 31ms across 74 paths, and
the two-hop crowding query 206ms. A graph engine earns its keep past
~100M edges or at unbounded depth; here it would cost a server or a heavy
dependency and buy nothing, while breaking the promise that `pip install
shunkan` is self-contained. So the MODEL is a graph (typed nodes, typed
sourced edges) and the STORAGE is relational - if it ever outgrows this,
callers do not change.

**Every edge names its source.** `put_edges` refuses a row without one.
The graph can always explain itself.

**The mapping service is the reason it joins at all.** Filings spell one
institution three ways. Normalisation handles form ("Ltd", "Through its
various schemes"); a curated CANONICAL list handles genuine synonymy
("LIC OF INDIA" = "LIFE INSURANCE CORPORATION OF INDIA"), written down as
auditable data rather than guessed by a fuzzy matcher that would also
merge things that are truly different. That merge alone recovered 20
positions LIC had split across spellings - the reverse map had been
undercounting.

**Self-contained.** The container cannot see `~/Projects` at all; it
serves ownership, funds, MSCI and the graph from its own store. The
sibling projects are import sources, not runtime dependencies.

## Mutual funds and MSCI: the flow layer (2026-08-24)

Two sibling projects on this machine already solved problems Shunkan was
about to re-solve, so the terminal imports their output into its own store
rather than duplicating their pipelines.

**Funds.** SEBI's shareholding pattern names the AMC ("SBI Mutual Fund,
6.96%") - the legal owner. The economic owner is a SCHEME, with a mandate
and a manager, and it is the scheme that trades. `data/funds.py` imports
the mfresearch pipeline's stores: 1,095 schemes, 1,087 disclosed
portfolios, 76,417 holding rows, of which 71,815 (94%) resolve to NSE
symbols through AMFI's own cap-classification list. The 4,602 that do not
are Treps, net receivables and repo - cash, not stocks - and they stay in
the store with a null symbol rather than being dropped, because a join
that hides its misses cannot be trusted. The payoff is the reverse
question: RELIANCE is held by 452 schemes worth Rs 1.35 lakh crore (index
ETFs on top, correctly); BALRAMCHIN by 68 worth Rs 2,967 crore (small-cap
funds, correctly). Company pages carry it beside the SEBI registry.

**MSCI.** Index membership is a flow event before it is anything else: an
addition forces every tracking fund to buy on one date at one price.
`data/msci.py` imports the local rule engine's constituent lists and
review predictions - 669 names screened, 103 moves called, each carrying
the rule that decided it (ADANIGREEN standard addition p=0.98; LAURUSLABS
small->standard migration). "hold" is filtered out of the move list
because 566 of them would bury the 103 that matter. The engine's call is
not MSCI's announcement and the page says so.

Both importers read from the sibling projects on the HOST; the container
sees the result through the shared store, and says exactly that when run
where the sources are not mounted.

## Ownership registry, and the reverse map (2026-08-24)

The first cut of the ownership panel had a real arithmetic bug, caught by
the owner reading it: promoter + institutions + public summed to 119.5%.
SEBI's tree does not work that way - PUBLIC ALREADY CONTAINS the
institutions. It now renders as filed: promoter + public = 100 (Reliance
50.48 + 49.52), with domestic / foreign / non-institutional nested inside
public. The list was also capped at 30 while Reliance files 57; the cap
is gone and the table is tabbed by bucket.

The "who are these anonymous LLPs" question is answered by the filing
itself: Companies Act s.90 Significant Beneficial Owner declarations sit
in the same XBRL and name the humans behind the shells (Srichakra
Commercials LLP -> Mukesh, Nita, Isha, Akash and Anant Ambani). 36 of
Reliance's 57 holders carry a control chain. Entity kind (LLP / company /
trust / HUF / fund / individual) is read off the legal suffix, because
the filing's PAN field is masked and nothing should be inferred from it.

Every scan persists to `store/ownership/holders.parquet`, which makes the
REVERSE question answerable without a new source: 60 companies scanned
gives 2,249 distinct holders and 4,386 positions, and LIC turns up in 45
of them (ITC 16.3%, L&T 12.4%). Coverage always travels with the answer -
a partial scan must never read as a holder's full book.

## Company intelligence and the SPLC map (2026-08-24)

The Bloomberg comparison, answered with what Indian disclosure actually
forces into the open.

**Ownership is no longer a refusal.** SEBI LODR Reg 31 publishes the
quarterly shareholding pattern as XBRL on nsearchives, not as a PDF, so
the promoter group is nameable entity by entity - individuals, family
trusts, holding companies - with public holders above 1% alongside.
Balrampur renders 30 named holders (Saraogi Family Trust 24.83%, SBI MF
6.96%...). The earlier "no free structured source" refusal was honest
when written and is retired now that the source is real.

**The SPLC map is evidence retrieval, not inference.** Annual reports
(LODR Reg 34) are on nsearchives back a decade; 400 pages parse in ~13s.
Every node on the map - input, output, customer, facility, family member
- carries the verbatim sentence it came from, and a node whose quote is
page furniture is dropped rather than shown. Nothing is summarised from
model knowledge: if the report does not say it, the map does not show it.
Verified on two unlike businesses (sugar and refining/telecom): the
Balrampur map finds ethanol going to oil marketing companies, industrial
alcohol to institutional buyers, and cogenerated power to UPPCL via the
State Electricity Grid; the Reliance map finds crude oil and natural gas
inputs, Jamnagar/Dahej/Hazira, and the Jio/Meta corporate family.

Known limits, stated on the page: third-party supplier NAMES with cost
shares are genuinely absent from Indian disclosure, so no supplier node
appears unless the report names one; and the extractor reads text, so an
image-only scanned report yields a named miss rather than a blank map.

## Research findings

**News-reaction study (2026-08-18).** First study off the news archive, 51
symbols, 547 shock events. Survives its own artifact check (symbol fixed
effects added after the raw run showed a coverage/size bias wearing a result's
clothes): no-news −2σ days bounce +36bp next day while negative-news drops stay
flat. Fails the cost bar (~25–30bp round trip) on one year of data. No trade;
rerun on the live channel when it has depth. Sentiment-sign prediction (H2):
dead. Details in `research/DECISIONS.md`.


Reproducible: `.venv/bin/python research/vrp_regime.py`, reads only the local
archive.

- **The Indian variance risk premium is real.** India VIX minus subsequent
  21-day realised vol: mean +2.946 vol points, positive on 79.7% of 4,484 days,
  t=5.75 on 214 non-overlapping windows, 2008-2026. Monotone in VIX level
  (+1.66 / +2.18 / +3.05 / +4.90 by quartile). Survives 2008 and 2020.
- **You cannot sell it naively.** ATM straddle IV runs at 0.927x the index (77
  observations reconstructed from listed contracts, 98.7% below 1.0). After
  that haircut the unconditional monthly short straddle is +13.3 pts, t=0.73.
  Not a trade.
- **The regime-gated version does NOT pass our own validators.** Rich-tercile
  looks strong on its own terms (+78.6 pts, NW t=2.98; +103 excluding 2020) but
  permutation gives p=0.036 (better than chance, not decisively) and deflated
  Sharpe gives DSR 0.454 against a generous 20 trials. Both must pass. It does
  not, and it is not being traded.
- **The tail is the whole story.** Worst rich-tercile window is Rs -148,196 per
  lot with skew -1.30. Any sizing has to be for that, not for the mean.

Net: a confirmed phenomenon and no confirmed way to harvest it. More capture
does not fix this; a better-identified signal might.

## Not started

- Global market execution. Read only context only, no feed beyond index quotes.
- Option backtesting. The instruments and chain archives only started
  accumulating on 2026-08-11, and expired contract history can't be
  back filled from Kite. This gets better every day it runs and cannot be
  rushed.
- Multi account or multi user anything.

## Things that are deliberately not features

- **No automated broker login.** Zerodha's terms require a manual login once per
  day. Shunkan doesn't work around that.
- **No estimated margins, lots or Greeks.** If a real number can't be sourced,
  the UI shows a dash. See the rule in the README.
- **No real order placement.** Paper only, and there's no code path to a live
  order.
