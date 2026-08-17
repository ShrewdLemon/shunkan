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
