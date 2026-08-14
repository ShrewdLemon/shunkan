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
  search over pure noise now fails.
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
- **Settling an expired position.** `Portfolio.settle_expired()` plus
  `POST /api/portfolio/settle`, at a price the trader types. The API path is
  tested end to end over HTTP; the SETTLE ticket has not been clicked in a
  browser.
- **Screener auto refresh.** `renderScreener` now repeats the last successful
  query every 5 minutes. Untested in a browser.
- **MCX, BFO, CDS.** The instrument model and margin lookup are venue aware and
  unit tested, but only NFO has been exercised against the live API. Commodity
  and BSE paths are code correct and unproven.

## Known rough edges

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
- **No settlement blotter.** The journal marks a settlement as asserted rather
  than executed, but nothing renders `portfolio.history`, so that distinction
  lives only in `~/.shunkan/portfolio.json` and the `/api/portfolio` payload.
- **Cold load is slow.** First paint of Pulse can sit on spinners for 15 to 20
  seconds while the initial fetches complete.
- **No accounts.** By design for a single user localhost tool. `shunkan serve`
  now refuses a non-loopback bind unless you pass `--i-understand-the-risk`,
  and when you do it mints a per-run token that every `/api/` call and the tick
  socket require. That is a guard rail, not a security model: it is one shared
  secret and there is still no notion of users.

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
