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

## Works but thinly tested

- **Order ticket.** The API path is verified end to end over HTTP. The actual
  click and keyboard interaction has not been exercised in a browser yet. If
  you hit something odd, that's why.
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

- **The LIVE badge is asserted, not observed.** `stream/factory.py` stamps
  `live=True` when the ticker constructs, not when a tick arrives. A dead token
  can show a green LIVE with a zero tick count. Being fixed.
- **No source timestamps on payloads.** The UI stamps the browser clock, so a
  number that's minutes stale looks current. Being fixed alongside the above.
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
