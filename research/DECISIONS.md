# Trade decision log

Every decision, including the decision not to trade. A journal that only
records trades teaches you nothing about the days you sat out, which for a
premium-selling programme is most of them.

Format: what the market was, what the model said, what was decided, why, and
what would have changed the answer. The last line is the important one — it is
the only way to tell later whether a decision was right for the right reason.

**These are paper decisions in Shunkan's own book.** Nothing here reaches a
broker and there is no code path that could. Nothing here is advice.

---

## 2026-08-17 (Mon) — NO TRADE

**Market, 14:18 IST**

| | NIFTY | BANKNIFTY |
|---|---|---|
| expiry | 2026-08-18 (1.0 DTE) | 2026-08-25 (8.0 DTE) |
| spot | 24,342.0 | 57,610.2 |
| ATM IV | 11.47% | 12.70% |
| ATM straddle | 119.5 | 869.5 |
| implied move | ±0.49% | ±1.51% |
| PCR (OI) | 1.21 | 0.85 |
| max pain | 24,350 | 57,700 |

India VIX **11.53** — the **5.5th percentile** of 4,483 sessions since 2008.

NIFTY ATM IV term structure: 11.47 (1d) / 10.17 (8d) / 10.24 (15d) / 10.40
(22d) / 10.47 (29d) / 10.55 (43d). Front elevated by expiry, then a shallow
contango.

Realised vol: 2.74% (5d), 3.84% (10d), 9.44% (21d), 11.18% (63d). The last six
sessions moved −0.46% at most. Verified against the raw archive that this is a
genuinely quiet tape and not a data gap.

**Decision: no position.**

**Why**

1. The only thing established beyond argument is that the variance risk premium
   exists (+2.946 vol pts, t=5.75, 214 non-overlapping windows). It is
   **monotone in VIX level**: +1.66 in the lowest quartile against +4.90 in the
   highest. We are in the lowest quartile. This is where the premium is
   thinnest and the tail is unchanged.
2. The regime gate reads **FLAT**: gap +1.51 against a +4.27 rich threshold.
   Tested at seven lookbacks from 5 to 63 days — flat at every one. This is a
   robust no, not a marginal one.
3. The gate failed our own validators anyway: permutation p=0.036, deflated
   Sharpe 0.454. And today's parameter sweep showed the reported t-stat
   **peaks exactly at the 21-day lookback that was chosen** (2.98, against
   0.92–2.37 elsewhere), which is the signature of selection rather than edge.
4. The one-day NIFTY straddle at 119.5 implies ±0.49% against 21-day realised
   of 9.44% (≈0.59% daily). Selling it is being paid roughly nothing to carry
   an overnight gap into expiry.

**What would have changed the answer**

- VIX above roughly 16, or the gap above +4.27, putting us in the rich tercile
  where the measured premium is +4.90 rather than +1.66.
- A validated signal. The regime gate is not one yet; it is a phenomenon with
  a parameter-selected wrapper.

**Cost note** — at these prices a 1-lot NIFTY straddle round trip costs ₹109 on
₹7,768 of premium (1.40%), and a 1-lot condor costs ₹202 on ₹3,250 of credit
(**6.22%**). Flat ₹20-per-order brokerage dominates at small size; the same
condor is 0.53% at 50 lots. Any structure here would have needed to clear that
before clearing the tail.

---

## 2026-08-17 (Mon), later — signal hunt: NOTHING TRADEABLE

Four research angles on the harvested option archive, each attacked by a
dedicated killer whose job was to destroy rather than improve. Roughly 1,356
honest trials across the session. Nothing survived, and the reason turned out
to be my own data rather than the statistics.

**The archive has survivorship bias and I did not see it.** A contract appears
in it for a past date only if it was still listed when the harvest ran, which
is selection on a future variable. Verified myself:

- 133,968 rows, 271 sessions back to 2025-07-14, and **zero** rows where
  date >= expiry. The archive has never observed a settlement.
- 69.8% of candles have zero volume, **56.5% have zero open interest** — the
  column the archive exists for.
- Only **26 of 271 sessions** contain any contract within 45 days of expiry.
  Median days-to-expiry in January 2026 was **711**.

So "13 months of option history" was about two months of real near-chain data
bolted onto eleven months of illiquid LEAPS that happened to survive to harvest
day. I presented that as a research asset. It is not one.

**What died** (all reproduced and killed by the workflow's own attackers):

- *Max pain gravity* — beaten by a zero-information strike-ladder placebo at
  every horizon, and 80% correlated with the trailing 5-day return. It is last
  week's move wearing a costume.
- *PCR-volume momentum*, the only candidate to pass a permutation gate
  (p=0.025) — it is a put/call ratio computed on ~28 surviving LEAPS. The
  near-dated version cannot be constructed at all.
- *OI as support/resistance* — 12,019 strike-days is 57 independent days
  wearing a costume; t moves from -1.44 to -5.86 on specification alone.
- *Skew carry, skew reversion, term-structure prediction* — every univariate
  result dies on a control for ATM IV, which it correlates -0.46 with.
- *Expiry-day straddle* — untestable. Zero settlements in the archive.
- *Day-after-expiry drift* — real before 2017, absent since (t=+0.20 for
  2020-2026), found by an 88-cell sweep.

**The least-dead thing**, named as such and not traded: NIFTY's overnight
versus intraday decomposition. +9.411 bps/night, t=11.01 over 19 years,
survives ex-2020 and both halves. It still fails on economics: +1.10%/yr over
buy-and-hold before slippage, negative for the last 500 sessions, and its gross
Sharpe of 2.57 against 0.19-0.98 in every other developed market is the kind of
unexplained magnitude where artifacts live.

**Decision: trade nothing. Fix the capture.** Harvest each expiry ON expiry day
after the close, before Kite drops the contract overnight. That is now wired
into harvest_loop. It does not recover the back-history, which was never really
there, but from here the archive accumulates unbiased near-chain data with
settlements in it.

---

## 2026-08-18 — news-reaction study: first result from the archive, one artifact caught

**Question (pre-registered in `research/news_reaction.py` before numbers were seen).**
H1: down-shocks WITHOUT named news reverse (liquidity), down-shocks WITH negative
news continue (information). H2: same-day sentiment sign predicts next days' sign.

**Data.** 51 NIFTY50/BANKNIFTY constituents, 22k archived headlines (backfill
channel, ~1yr window), 547 pooled ±2σ shock events on 185 distinct dates.
Outcome: excess log return vs NIFTY at +1/+3/+5 closes.

**Artifact caught before it became a finding.** First run showed BOTH no-news
buckets positive at every horizon — down-shocks "reversed" AND up-shocks
"continued". A directionless positive on the same bucket is not an asymmetry;
it is bucket membership correlating with coverage, coverage with size, and size
with the year's alpha vs cap-weighted NIFTY. Fix: subtract each symbol's mean
daily excess drift over the window (symbol fixed effects). The tables shrank.

**What survives demeaning.**
- Down-shock + no indexed news: +0.36% next day (t=2.76 pooled, 123 events on
  66 distinct dates → clustering haircut puts effective t nearer 2.0).
- Down-shock + negative named news: flat at every horizon (+0.06/−0.03/−0.01).
  The spread is H1's direction: news-backed drops stay repriced, no-news drops
  partially bounce.
- BUT up-shock + no news also drifts UP (+0.48% at +5d, t=1.89). A pure
  liquidity-reversal mechanism predicts those revert. It doesn't fit; either
  the down-side effect is forced-selling-specific (plausible, documented in
  other markets) or the none bucket still carries a residual bias.
- H2 is dead. The keyword sentiment buckets do not order outcomes anywhere.
  No surprise: it counts words.

**Tradeability math.** Buy-at-close on a no-news −2σ day, exit +1 close:
+36bp gross. Delivery costs on NSE large caps ≈ 25–30bp round trip (STT 0.1%
both sides dominates; brokerage nil). Net ≈ +6–11bp on one year of data with
effective t ≈ 2. That is a watchlist entry, not a trade.

**Decision: NO TRADE.** Logged as the first candidate that survives its own
robustness check but fails the cost bar. Re-run when the LIVE channel (unbiased,
no Google-index attrition) has accumulated 6+ months — the stated bias of the
backfill channel works AGAINST H1, so the live rerun is the honest test and
could strengthen it.

**Addendum, same day (backfill completed to 59/59 symbols).** Rerun at full
width: 630 events, 194 distinct dates. Down+none +1d: +0.35% (t=2.99, 140
events / 68 dates) — unchanged from the 51-symbol run. Down+neg stays flat.
The up+none upward drift also persists (+0.52% at +5d, t=2.09), so the
asymmetry puzzle is stable, not a small-sample wobble. Verdict unchanged:
direction real, size below costs, revisit on the live channel.

## 2026-08-18 — expiry-day session read (weekly NIFTY settles today)

Live snapshot ~12:05 IST, all real (token restored mid-morning; capture
clean since):

- NIFTY 24,210 (−0.32%), red day, z = −0.45: ordinary, no event.
- VIX 11.65 = 6th percentile of the 2008+ series. rv21 9.3%.
- Expiry board: spot 24,200.05 vs max pain 24,200 — ON the pin. PCR 0.645,
  ATM IV 17.2% (0-DTE annualisation, read the straddle instead): ₹66.95,
  0.28% implied to settlement.
- Participants (17 Aug file): Client and DII both added bullish exposure,
  FII stayed net short index futures. News bias mildly bullish on 8 items.

The tempting trade is the one every expiry-day thread sells: the pin
straddle. The framework says no, twice over. First, VIX in its bottom
quartile is where measured VRP is thinnest (+1.66 pts in Q1 vs +4.90 in
Q4), and the unconditional straddle short already failed the 0.927
IV/VIX haircut. Second, we have never backtested intraday 0-DTE decay
against settlement prints, because until TONIGHT the store has zero
settlements — that dataset starts existing today, with the first
settling-series harvest after the close plus the day's 60s chain capture.

**Decision: NO TRADE.** The correct expiry-day move with this
infrastructure is to let it record. Every future expiry gets a settlement
row; when there are ~10, the 0-DTE decay question becomes testable
instead of folklore.

## 2026-08-18 — participant-positioning screen: the folklore is dead (1yr)

Pre-registered in `research/participant_signal.py`: does day t's change in
FII/Client/DII/Pro direction-sign net exposure (futures and options
separately, 8 series) predict NIFTY traded honestly at the next open
(open t+1 → open t+2)? 255 published days, 2025-08 → 2026-08.

Result: nothing. Every sign-read spread |t| ≤ 1.3; no quartile
monotonicity anywhere (zigzags, not gradients); the FII futures sign
points the WRONG way for the folklore (−9.1bp after "FII added bullish").
"FII bought index futures today" carried zero next-day information this
year at daily horizon. Every market wrap implying otherwise is narrating
noise — which is what it looked like from the start, but now it is a
number in a log and not an opinion.

Depth check queued: the NSE archive serves years back; a 4-year pull into
a research-local root (single-writer rule: the container owns the live
participant store) will either make this negative definitive or resurrect
a subtler read. Screen counted ~16 looks; anything later built from a
"survivor" here owes DSR that number.

**Decision: NO TRADE, and stop treating participant CHANGE as a daily
directional input in the analysis panel's verdict — it stays displayed as
positioning fact, which is what it is.**

**Addendum, same day — the 4-year verdict.** Deep backfill landed: 1,011
published days (2022-07 → 2026-08, zero failed fetches), 989 aligned with
prices. Same pre-registered screen, same tradeable next-open path: best
|t| across all 8 series and both reads is 1.63 (Pro futures) — exactly the
best-of-16-looks noise draw. FII index futures, the series every evening
wrap narrates, sits at t = −0.74 with a mildly inverted quartile ladder.
The daily-horizon folklore is now dead at four years of depth, not one.
Anything resurrected from this table later (weekly horizons, levels
instead of changes, interactions with VIX regime) starts life owing DSR
its full trial count, this screen included.

## 2026-08-18 — dogfood session: Monday's plan graded, live stance unchanged

Full write-up published as the desk-reports artifact. Short form: Monday
evening's reconstruction (from the stores alone) called a range/pin regime
(HELD — 0.39% full-day range), a pull to max pain 24,350 (FAILED — pain
re-anchored to 24,200 by late morning and price never visited 24,350),
24,300 put support (MIXED — broke at the open, then capped the day). The
meta-lesson is structural: T−1 expiry maps die at the open; the tradeable
object is intraday wall migration, which the capture records for the first
time TODAY (76 snapshots; morning hole 09:15–11:45, token was dead).

Live stance 13:10 IST: NO TRADE reaffirmed. Spot 24,188 vs pain 24,200,
straddle ₹56 (±0.23%), puts +147L built at 24,150/24,200 = crowded short-put
side, break of 24,150 is the asymmetric scenario. SABR on 25 Aug: ATM 9.25%
vs rv21 9.4% — VRP flat at one week, the premium is not there to sell.

## 2026-08-18 evening — seven days of the analysis, graded systematically

`research/grade_analysis.py` runs the manual plan-vs-outcome exercise for a
window: each day D's analysis via the app's own replay endpoint, claims
scored against D+1's archive. Week of 08-07 → 08-18:

- **Regime call 7/7.** Ordinary day + VIX < 25th pctile → next move < 1σ,
  every time (with the stated base-rate tailwind of quiet begetting quiet).
- **Pain pull 1/5 — inverted.** Four of five days closed FARTHER from T-1
  max pain (worst: 62→195 pts into expiry). The magnet story is dead on
  this tape; the level is a trailing artifact.
- **Walls 11/12.** OI support/resist respected as RANGE BOUNDS every day
  except the expiry-morning break of 24,300. Levels yes, direction no.

Desk rule extracted: trust wide OI walls as bounds, ignore pain pull as
direction, and trust neither on expiry mornings until the migration
re-forms them. One week, small n, stated. Today's journal entry is the
first graded against a RECORD instead of a reconstruction - tomorrow.

## 2026-08-19 ~11:15 IST — pre-registration: first live forward test of the
## no-news shock bounce (PAPER)

Context: seventh straight red session; catalyst is macro (crude surge +
bond-yield spike per the morning wires); NIFTY broke the old 24,150 wall at
10:04 (the armed alert fired at 24,051); VIX 11.5 = 4.8th pctile; EIGHT
constituent shocks <= -2sigma by 11:05 (ASIANPAINT -4.6z, SBILIFE, ITC,
TMPV, SUNPHARMA, COALINDIA, CIPLA, ULTRACEMCO) with NO title-tagged news -
one clustered macro event, which the study's caveats anticipate.

THE RULES, fixed before the close:
- At 15:05 IST, re-qualify: intraday return still <= -2 trailing-63d sigma
  AND still no company-named headline in the archive (title-tag).
- Enter AT CLOSE (paper book, cash equity, 1 unit each, equal rupees
  ~1 lakh notional total across qualifiers). Exit at tomorrow's close.
- Expected per the study: +35bp mean gross, ~55% hit; costs on paper are
  recorded at the delivery stack from costs.py for honesty.
- This is ONE clustered event = effectively ONE draw, not N independent
  trades. It tests execution mechanics and the signal's live behaviour; it
  proves nothing statistically on its own and will not be graded as if it
  did.
- NO index trade, NO vol trade: nothing validated, VRP at the thin end.

## 2026-08-19 15:14 — AGGRESSIVE SESSION MANDATE (owner's instruction, paper)

Owner: "trade very aggressively (on paper) to generate max profits in the
current session." Complying as a quant: every position carries the day's
own data as its reason, sized big, and every one of these is an
UNVALIDATED tactical call — logged as such, graded without mercy.

**A. Relative-strength pair (session trade, close by 15:38):**
LONG 90 BANKNIFTYFUT @ 57,350 (₹51.6L) / SHORT 260 NIFTYFUT @ 24,113.1
(₹62.7L). Reason: banks −0.08% on NIFTY's 7th red day (−0.4%) = clear
session relative strength; residual ₹11L net-short tilt kept deliberately
(day trend down, wall break risk). Exit at ~15:38 marks.

**B. Wall-defense bull put spread (carried, defined risk):**
SHORT 260× NIFTY 25AUG 24,000 PE @ 69.95 / LONG 260× 23,900 PE @ 42.40.
Credit ₹7,163, max risk ₹18,837. Reason: put writers added +39L/+49L/+58L
at 24,000/24,050/24,100 today (fresh, 13-21x vol/OI); walls graded 11/12
as bounds last week. Selling the defended level WITH protection one floor
lower. Known counters: VRP thin (VIX 4.8pctile), 7-day grind, crude
catalyst live — hence the long leg.

**C. Shock basket (pre-registered this morning):** 7 qualifiers book at
the CAS auction close ~15:37. COALINDIA dropped (tagged news) — the rule
worked.

Margin: reported not enforced on paper (owner's design). Gross notional
~₹1.14Cr on ₹1.0L equity — stated plainly, this is the aggression knob.
