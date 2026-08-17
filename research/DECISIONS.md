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
