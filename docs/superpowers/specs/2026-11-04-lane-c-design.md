# Lane C: paper-versus-replay gap (P6) and the options backtest (P5)

Date: 2026-11-04 · Branch: `lane-c-algo` (from `main` fe346a6) · Public edition

Requirements: `shunkan-desk/docs/lanes/2026-11-04-lanes.md`, "Lane C".
Pre-registration: `docs/superpowers/specs/2026-09-29-fo-bhav-vrp-design.md`
(carried unchanged from `fo-bhav-vrp` c499a8e). Its addendum is in Part 3 of
this file and is fixed before any expired option price is read.

## Part 1 — P6: the harness on `main`

Done: `algo-harness` (c09f544, 2f3f955) cherry-picked clean onto `main` as
166e954 and 6bd46e1. 629 tests pass; the boundary tests pass with
`SHUNKAN_PRIVATE_WORDS` set.

### What the gap measures, and three defects that would corrupt it

Paper and replay run the same `Engine` and the same fill model. A gap between
them can only come from timing, dropped ticks, or a difference in setup. So the
gap tests determinism and setup; it does not test whether the fill model is
true. Nothing does that short of live orders, which are out of scope.

Defects in the shipped comparison:

1. `cmd_compare` replays with no governor. Paper ran with one. Any order the
   governor refused live is filled in replay.
2. The governor reads the wall clock. Replayed after hours, every order is
   "outside session". A governed backtest is wrong at any hour but trading hours.
3. The report keeps neither the limits nor the strategy parameters, so a replay
   cannot rebuild the run that produced it.

### Changes

| Piece | Change |
|---|---|
| `risk.RiskGovernor` | `use_clock(fn)`: a public way to set the clock. |
| `engine.Engine` | `BacktestConfig.event_clock: bool`, default True. When True, `Engine.__init__` sets the governor's clock to the engine's event time (`ts_ns / 1e9`), for paper and replay alike (ruling R1). Paper's event time is the arrival stamp. |
| `paper.PaperConfig` | `watch: list[int]`: tokens recorded but not traded. One socket carries the traded token and the option chain tape. `on_ticks` steps the engine only for traded tokens. |
| `paper.save_report` | Adds `limits`, `strategy_params`, `tokens`, `watch` and a fill log (`ts_ns, token, side, price, qty, maker`). |
| `paper.replay_gap(session, root)` | Rebuilds strategy, limits and tokens from the report, replays with `event_clock`, and returns a `Gap`. |
| `Gap` | The existing `Divergence` fields plus: fills matched one-to-one (same side, price, qty, `|Δts| ≤ 50 ms`), first unmatched fill time, recorder drops, ticks seen live versus replayed. |
| `__main__` | `paper --watch-chain N` resolves the nearest NIFTY weekly at ±N 50-point strikes (CE and PE) plus the NIFTY 50 and India VIX index tokens. `compare` uses `replay_gap` and appends one line to `ticks/gaps.jsonl`. |

### Sessions

- Five market days. Each: `paper --symbols <NIFTY current-month FUT>
  --strategy quote-both-sides --watch-chain 10 --until 15:30
  --max-gross-exposure 6000000 --max-orders-per-day 50000 --max-loss 50000
  --max-drawdown 75000`, then `compare`. The default `RiskLimits` are sized
  for options; a future lot is about ₹16.6 lakh, so the default ₹2 lakh
  exposure cap would refuse every order. Rate caps stay at 8/s and 120/min.
- One traded token per session. A relaunch takes a new session name
  (`<date>-paper-2`), never the old one.
- Home: `SHUNKAN_HOME=~/.shunkan-lanec`. The Kite key and token are read at
  runtime from `~/.shunkan/credentials.json` (read-only) into
  `KITE_API_KEY`/`KITE_ACCESS_TOKEN`. Neither is printed.
- Needs the owner's Kite login (`shunkan connect zerodha`) each market morning
  before 09:15.
- Done when `gaps.jsonl` has five sessions. A session with recorder drops is
  kept and flagged, not replaced.

## Part 2 — P5: the multi-leg backtester (public)

`src/shunkan/backtest/options.py`, end-of-day, any number of legs, one expiry.

| Unit | Does |
|---|---|
| `Leg(right, strike, qty)` | `qty` is signed units per structure unit: +1 long, −1 short. |
| `DailyChain(frame, expiry)` | Rows of the `fo_bhav` frame for one symbol and expiry. `quote(day, strike, right) -> (close, contracts) or None`. |
| `nearest_traded(chain, day, target, right, step, direction, max_steps)` | Rounds `target` to the `step` grid, then walks `direction` until a strike traded that day. |
| `parity_forward(chain, day, spot)` | ATM = the listed strike nearest spot where CE and PE both traded; F = K + C − P. |
| `straddle_vol(F, K, straddle, T)` | Black-76 straddle inversion, r = 0, by bisection. |
| `mark(chain, day, legs)` | Σ qty·close, or None if any leg did not trade. |
| `simulate(chain, legs, entry, expiry, exit_days, settle, slippage, exit_rule, lot)` | Opens at the entry closes ± slippage, asks `exit_rule(credit, cost_to_close)` on each exit day with a full mark, closes at closes ± slippage, else settles at intrinsic. Returns a `TradeResult`. |
| `TradeResult` | entry, exit day, exit kind (`settle`, `take`, `stop`), gross, slippage, charges and net, all per unit in index points. |
| Charges | `backtest/costs.py` on every fill at one lot. Settlement charges exercise STT on ITM longs only, with no brokerage (cash-settled). Converted to per unit by dividing by the lot. |

Research scripts, each with pure functions plus `main()`:

- `research/vrp_condor.py`: expiry calendar, HAR-RV, VRP gate, condor builder,
  12 configurations × 3 slippages, walk-forward, pass rule, reports.
- `research/pcr_daily.py`: the 8 PCR tests.

Tests run on synthetic chains only (`tests/test_options_backtest.py`,
`tests/test_vrp_condor.py`, `tests/test_pcr_daily.py`). The cases are the ones
the pre-registration lists, plus HAR no-lookahead, walk-forward selection, and
PCR outcome alignment.

Data: lane A's `shunkan.data.fo_bhav.load_range(start, end, symbols=["NIFTY"],
kinds=["OPTIDX"])`. The research script caches the NIFTY option rows in
`$SHUNKAN_HOME/research/nifty_opt.parquet`, never in `~/.shunkan`. No loader is
written here.

### Run protocol (no peeking)

1. All code and tests are built on synthetic data and reviewed. Then the commit
   is frozen.
2. **Coverage audit.** It prints counts only: expiries found, entries built,
   weeks skipped and why, untraded legs, missing settlement closes. It prints
   no prices, P&L, σ or VRP. Data-handling bugs it exposes may be fixed.
3. **One run** of each script at a recorded commit. Its output is saved and its
   verdicts are logged in `research/DECISIONS.md` with that commit hash.
4. Any later code change is logged as such. A rerun after a bug fix is reported
   next to the first run, never in place of it.

## Part 3 — addendum to the pre-registration (fixed 2026-11-04, before data)

Where the pre-registration is silent, these resolutions are fixed. Items marked
**owner** need the owner's choice before step 3.

### Iron condor

1. **Sample.** Consecutive NIFTY expiry pairs (E_prev, E_next) where E_prev ≥
   2019-02-14 (the first weekly expiry). Expiries are the dates on which NIFTY
   options of that expiry appear in that day's file. Entry is at E_prev's close,
   selling E_next.
2. **T.** The sessions after entry up to and including E_next, divided by 252.
3. **Forward and σ.** The ATM is the listed strike nearest the NIFTY spot close
   (`NIFTY.parquet`) where CE and PE both traded; F = K + C − P on closes.
   σ = Black-76 inversion of C + P at K, with r = 0.
4. **Strikes.** Round F·e^(±σ√T) and F·e^(±2σ√T) to the nearest 50. A strike
   that is unlisted or untraded steps outward by 50, at most 10 times. Wings
   must lie strictly beyond the shorts. If no valid structure exists, or the
   gross credit is ≤ 0, the week is a zero row with a logged reason.
5. **HAR-RV.** Daily variance v_t = GK_t + ln(O_t/C_{t−1})², where
   GK = ½ln(H/L)² − (2ln2−1)ln(C/O)².
   - Target: the mean of v over the h holding sessions.
   - Regressors at t: v_t, the 5-day mean and the 22-day mean.
   - OLS with intercept, refit at each entry on every t since 2007-09 whose
     target window ends on or before the entry date. h is that week's.
   - Forecast floored at 1e-10. σ̂ = √(252·forecast).
   - VRP = 100(σ − σ̂) in vol points.
   - θ = none means the week always trades; otherwise the week trades when
     VRP > θ.
6. **Exits.** Decisions are taken at the close of each session strictly between
   entry and expiry, and only when all four legs traded. Expiry day settles.
   On gross closes, with C the gross entry credit and X the cost to close:
   - E1 takes profit when X ≤ 0.5·C.
   - E2 also stops when X − C ≥ 1.5·C.
7. **Slippage.** Sells fill at max(close − s, 0); buys fill at close + s.
   Settlement has none.
8. **Settlement.** Intrinsic against the NIFTY close on E_next.
9. **Charges basis (owner).** Recommended: one lot of 65 per fill, divided by
   65. That is 8 orders at ₹23.60 each, about 2.9 points a week before taxes.
   The alternative is 10 lots, which cuts brokerage per unit tenfold. This
   choice can decide the verdict, which is why it is fixed now.
10. **Week assignment.** A week belongs to the year of E_next. Each January's
    selection uses every week that expired before that year. The
    out-of-sample period is the weeks expiring 2022-01-01 to 2026-09-30.
11. **Selection.** The highest annualised Sharpe (52) of weekly net P&L at
    s = 0.5, zero rows included. Ties go to the first in the order θ (none, 0,
    2, 4) × exit (E0, E1, E2).
12. **Pass criteria on the stitched out-of-sample series.**
    - (1) NW t uses lags 4; the statistic is t ≥ 2.0.
    - (2) `validate.deflated_sharpe(series, n_trials=15, periods=52)`, passing
      at ≥ 0.95. **Owner:** `trial_sharpe_std`. Recommended: the standard
      deviation of the 12 configurations' annualised out-of-sample Sharpes at
      s = 0.5 ("read from the search itself", as `trials_of` does). The
      alternative is the house fallback of 1.0, under which a best-of-15 needs
      an annualised Sharpe near 1.8 just to clear zero skill.
    - (3) The same selected path, repriced at s = 1.0.
    - (4) Split by E_next: before 2024-11-20, and on or after.
13. **Breakdowns.**
    - VIX terciles use `_INDIAVIX.parquet` closes at entry, with cut points over
      the full sample.
    - The eras are Thursday expiries (E_next < 2025-09-01) and Tuesday expiries
      (from then).
    - The straddle comparison reports the short ATM straddle held to settlement,
      and the mean of σ_ATM/VIX against 0.927.

### Daily PCR

14. **Nearest weekly.** The nearest NIFTY expiry strictly after t. "All" means
    every NIFTY expiry on t.
15. **z-score.** Against the 60 sessions t−60 … t−1. The change is PCR_t −
    PCR_{t−1}, on the definition-level series.
16. **Returns.** ln(O_{t+2}/O_{t+1}) and ln(O_{t+6}/O_{t+1}), from `NIFTY.parquet`
    opens, aligned by position on the joint session index.
17. **Statistics.**
    - IC is the Spearman correlation. Its NW t is the slope t of
      rank(return) on rank(signal), with lags h (1 or 5).
    - The partial IC uses the same regression after both ranks are residualised
      on the ranks of ln(C_t/C_{t−1}) and ln(C_t/C_{t−5}).
    - Sample: every session with a full window, 2019-01 to the last session
      with t+6 data.
18. **Survival.** Holm-adjusted p < 0.05 with |t| ≥ 2.5, on both the raw and
    the partial IC. The stricter reading is used where the registration is
    silent.

## Out of scope

- Live orders.
- Intraday option fills: the chain tape recorded here is their future input.
- Any re-tuning after step 3.
