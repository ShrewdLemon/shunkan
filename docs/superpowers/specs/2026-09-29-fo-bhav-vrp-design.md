# NSE F&O archive + pre-registered VRP condor and daily PCR tests

Date: 2026-09-29 · Status: awaiting owner approval · Branch: `fo-bhav-vrp`

This document is also the pre-registration. No choice below was made after
looking at an expired option price. The only archive files read so far are three
format probes: 2019-02-15, 2024-06-28 and 2026-09-28.

## Why

- **Prior signal hunt.** The 2026-08-17 hunt in `research/DECISIONS.md` died on
  one root cause: the harvested option archive is survivorship-biased and has
  never seen a settlement.
- **Prior VRP study.** `research/vrp_regime.py` found the variance premium real.
  It could only price the premium through India VIX, with the measured 0.927
  ATM-to-VIX haircut.
- **Today's spike.** It used Kite 5-minute OI, Sep 23–29. Intraday PCR and OI
  flow showed no edge. The weekly iron condor proxy made +17 pts/week (t = 1.4),
  but it was also priced off VIX, so that figure is an upper bound.
- **What NSE publishes.** The daily F&O bhavcopy lists every contract that
  traded or held OI that day, expired ones included. It carries close,
  settlement price, OI and volume.
- **Coverage, probed today.** The legacy format serves 2019 and earlier. The
  UDiFF format serves 2024-07-08 onward. Together they cover every NIFTY weekly
  expiry since the first one in February 2019, about 395 of them. Each is priced
  from real trades and settled, with no survivorship.

## Approaches

1. **Recommended: archive on `main`, research in `research/`.**
   - A data module shaped like `data/participant.py`, stored under
     `~/.shunkan/store/fo_bhav/`.
   - Two research scripts that reuse `backtest/costs.py` and
     `backtest/validate.py`.
   - Verdicts logged in `DECISIONS.md`.
   - The terminal can later reuse the same archive for PCR, OI and IV history.
2. **Build it on `algo-harness`.** Rejected: that branch is unmerged and behind
   `main`, and the archive is a data asset, not harness code.
3. **Keep it in a scratchpad.** Rejected: every number in `DECISIONS.md` must
   reproduce from a committed script.

## Part 1 — archive: `src/shunkan/data/fo_bhav.py`

| Piece | Behaviour |
|---|---|
| `url_for(day)` | From 2024-07-08, the UDiFF file `content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip`. Before that, the legacy file `content/historical/DERIVATIVES/{YYYY}/{MON}/fo{DD}{MON}{YYYY}bhav.csv.zip`. On a 404, try the other form. |
| `fetch(day)` | Keeps the raw zip in `~/.shunkan/store/fo_bhav/raw/{YYYY}/`. A 404 on both forms writes a `{YYYYMMDD}.missing` holiday marker. Other errors retry with backoff and are never marked. Headers and pacing follow `participant.py`. |
| `parse(path)` | Turns both formats into one frame: `date, kind, symbol, expiry, strike, right, open, high, low, close, settle, contracts, oi, oi_change, underlying, lot`. |
| `sync(start, end)` | Resumable backfill. Rows for the index derivatives (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50) go to `index_{YYYY}.parquet`. Stock derivatives stay in the raw zips. The calendar is the NIFTY sessions in `store/history/NIFTY.parquet` plus every weekday. |
| CLI | `shunkan fo-bhav sync [--since YYYY-MM-DD]` |

Parsing notes:

- UDiFF instrument types `IDO/IDF/STO/STF` map to
  `OPTIDX/FUTIDX/OPTSTK/FUTSTK`.
- Units were checked on the 2026-09-28 file. Volume is in contracts and OI is in
  units: the 22600 PE shows 1,690,000, which is 26,000 lots of 65.
- `underlying` and `lot` exist only in UDiFF.

Size and status:

- About 1,950 files and 1.4 GB raw: 0.45–0.6 MB a day in the legacy format and
  1.3 MB a day in UDiFF.
- The index parquet is tens of MB.
- The raw mirror is already running from a throwaway script with this same
  layout, so `sync` will find the files cached.

## Part 2 — research A: weekly NIFTY iron condor (`research/vrp_condor.py`)

**Sample.** Every NIFTY weekly expiry from February 2019 to September 2026.

- Expiry dates come from the data, not a calendar.
- The expiry weekday moved from Thursday to Tuesday on 2025-09-01.
- Holidays shift single weeks.

**Entry.** Positions open at the close of the previous weekly expiry day, selling
the next weekly, which normally has five sessions left.

- **Forward.** F comes from put-call parity at the ATM strike.
- **Vol.** σ comes from the ATM straddle close by Black-76 inversion, in
  trading-day time.
- **Short strikes.** The 50-point strike nearest F·e^(±σ√T), about 16 delta.
- **Wings.** The same rule at ±2σ√T.
- **Liquidity.** Every leg must have traded that day; if one did not, step one
  strike further out.

**Signal.** VRP = σ − σ̂, and the week trades only when VRP > θ.

- σ̂ is a HAR-RV forecast of realised vol over the holding window, with daily,
  5-day and 22-day components.
- Its inputs are daily Garman–Klass variance plus the squared overnight return,
  from `NIFTY.parquet`.
- It is fit by OLS on data before the entry date only.

**Exits.** Three variants:

- **E0.** Hold to settlement.
- **E1.** Buy back at 50% of the credit at any daily close, otherwise settle.
- **E2.** E1, plus a stop when the mark-to-market loss reaches 1.5× the credit
  at a daily close.

Marks use only legs that traded that day. A day with an untraded leg makes no
exit decision.

**Settlement.** Legs settle at intrinsic value against the NIFTY close on expiry
day, which is the index-option final settlement price. ITM longs pay exercise
STT on intrinsic.

**Costs.**

- **Charges.** `backtest/costs.py` at today's statutory rates for the whole
  sample. That is conservative before October 2024, when STT on option sales was
  lower.
- **Slippage.** s per leg per fill takes the values {0.25, 0.5, 1.0} points.
- **Why 0.5.** Measured today on the Oct 6 weekly, the half-spread was 0.10 at
  the ±1σ strikes and 0.05–0.07 at the ±2σ wings. Decisions use s = 0.5, five
  times that.

**Unit.** P&L is per unit, in index points, with one row per expiry week. A week
the gate skips counts as zero. Rupees are shown at today's lot of 65.

**Trials and selection.**

- θ ∈ {none, 0, 2, 4} vol points × {E0, E1, E2} gives 12 configurations.
- The walk-forward is anchored. It trains on February 2019 to the end of 2021,
  then trades 2022 with the configuration that had the best in-sample Sharpe of
  weekly net P&L.
- Every January it re-selects on all prior data.
- The out-of-sample period is 2022-01 to 2026-09, about 245 weeks, and it runs
  once.

**Pass rule.** All four must hold on the out-of-sample series:

1. Mean weekly net P&L > 0 at s = 0.5, with a Newey–West t ≥ 2.0 (4 lags).
2. Deflated Sharpe probability ≥ 0.95, with n_trials = 15: these 12 plus the
   spike's 3 proxy structures.
3. Mean > 0 at s = 1.0.
4. A positive mean both before and after 2024-11-20, when SEBI's weekly-expiry
   changes took effect.

If any one fails, the result is a `DECISIONS.md` entry and no trade. There is no
re-tuning on the out-of-sample.

**Reported, not traded.**

- **Straddle comparison.** Over the full sample, the same weeks priced as an
  unhedged short straddle. This is compared with the spike's VIX proxy and with
  `vrp_regime.py`'s 0.927 haircut.
- **Breakdowns.** Every configuration split by VIX tercile and by the Thursday
  and Tuesday eras.
- **Tail risk.** CVaR 5%, worst week and maximum drawdown for all of the above.

## Part 3 — research B: daily PCR (`research/pcr_daily.py`)

- **PCR.** Put OI / call OI at close t, on two definitions:
  - the nearest weekly with at least one session left;
  - all NIFTY expiries.
- **Transforms.** The level as a z-score against the trailing 60 sessions, and
  the one-day change.
- **Outcome.** Returns follow the tradeable path. The file publishes after the
  close, so they run from open t+1 to open t+2 and to open t+6. They are aligned
  on the trading-day index, as `participant_signal.py` does.
- **Stats.** Spearman IC with a Newey–West t, and a partial IC after trailing
  1-day and 5-day returns.
- **Threshold.** 2 × 2 × 2 = 8 tests, Holm-corrected, against the house bar of
  |t| ≥ 2.5.
- **Survivors.** Any survivor gets its own pre-registration before a strategy is
  built on it.

## Testing

`tests/test_fo_bhav.py`:

- URLs for both eras, and the fallback between them.
- Legacy and UDiFF fixture rows parse to the identical frame. This includes
  legacy `21-Feb-2019` dates and the `XX` right on futures.
- A 404 writes the holiday marker; any other error does not.

Research A:

- The settlement payoff against hand-computed cases.
- Strike rounding and the traded-leg rule.
- Costs charged per fill.
- One synthetic week, end to end, with a known P&L.

## Out of scope

- **Intraday stops and fills.** End-of-day data cannot see them; they need ticks
  from the algo-harness recorder.
- **Paper and live trading.** If A passes, a separate spec puts it into the algo
  harness's paper mode behind the risk governor. There, size comes from
  fractional Kelly on the maximum loss. Going live stays the owner's call.
- **Other underlyings.** BANKNIFTY and stock options are stored but not
  researched.

## Honest limits

- A bhavcopy close is a last trade, not a mid, and far wings can be stale. The
  traded-leg rule and the slippage grid bound this; they do not remove it.
- Daily stop checks miss intraday paths and understate losses when the market
  gaps.
- 2019–2024 was a different weekly market: Thursday expiry, weeklies on several
  indices, and different lot sizes. Pass criterion 4 stops a result carried only
  by that era from passing.
- `INDIAVIX.parquet` ends 2026-08-11. Refresh it before the tercile split.
