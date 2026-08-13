# Shunkan 瞬間

[![tests](https://github.com/ShrewdLemon/shunkan/actions/workflows/tests.yml/badge.svg)](https://github.com/ShrewdLemon/shunkan/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)

A derivatives terminal for Indian markets. Built for people who already know
what they're looking at: option chains with real open interest, a position book
that can actually go short, net Greeks across the whole book, and exchange
priced margin instead of a guess.

Runs locally. `shunkan serve` opens a web terminal on 127.0.0.1. Your broker
credentials never leave your machine.

```
pip install -e .
shunkan connect zerodha     # one browser login, once per trading day
shunkan serve
```

### Or run it in Docker

If you would rather not start it from a terminal every day:

```
docker compose up -d
```

Then open http://127.0.0.1:8720. It restarts on boot and after a crash, and
`~/.shunkan` lives in a named volume so credentials and the archive survive.

Two things about the compose file worth not changing casually. The ports are
published to `127.0.0.1` only, because Shunkan has no accounts and a wider
bind hands a live broker session to your network (and, under Zerodha's terms,
their market data with it). And port 8722 is published because Kite redirects
the login to `127.0.0.1:8722/callback` on the host; without it
`shunkan connect` waits forever for a request that can never arrive.

The daily token still needs you once each morning. In the container the login
URL is printed rather than opened, so copy it into a browser:

```
docker compose exec shunkan shunkan connect zerodha
```

*Shunkan (瞬間) means "the instant". Hot paths are pure numpy vector code with
latency budget tests enforcing it: full chain Greeks in microseconds, 10 year
backtests in single digit milliseconds.*

## The one rule that shapes everything

**No fabricated number ever renders as real.**

This sounds obvious and almost nothing gets it right. The usual failure is
subtle: a terminal can't reach the exchange, quietly falls back to a model, and
draws the model in the same font as the truth. You end up reading max pain off
simulated open interest and sizing a trade on it.

Shunkan refuses instead. If the chain can't be sourced you get a card naming
every source it tried and why each one failed, not a stand in book. Concretely:

- Can't reach a chain? Refuse, with the source trail. No synthetic fallback on
  the live path.
- No lot size from the instruments dump? `lot_size` is `None`, money is shown
  per unit, and the UI says why. It never assumes 50, or 75, or anything.
- No previous-day open interest to difference against? The ΔOI column shows
  dashes and the header says "no stored snapshot yet", rather than zeros that
  look like "no change".
- Can't price margin for every leg? `margin_used` is `null`. A total that
  silently omits a leg reads as complete and understates your risk.
- A position with no chain to mark against? It gets named and excluded from net
  Greeks, never counted as zero.

If you find somewhere this rule is broken, that's a bug worth filing.

## Who it's for

Traders working across equity, index and commodity derivatives on Indian
venues, who don't need theta explained to them. Dense tables, tabular numerals,
keyboard first, no chrome. The design target is Bloomberg discipline rather than
a dashboard.

Global markets are read only context right now. There's a Pulse board with US,
European and Asian indices for cues, but no execution path outside India.

## Setup

### Requirements

Python 3.12+ and a Zerodha account with a Kite Connect subscription. It has to
be the paid Connect app, not the free Personal one, which returns
`PermissionException` on market data calls.

### Broker

1. Create an app at [developers.kite.trade](https://developers.kite.trade),
   type **Connect**.
2. Set the redirect URL to exactly `http://127.0.0.1:8722/callback`. This is
   hardcoded in `data/brokers.py` and a mismatch fails silently, which is a
   miserable thing to debug.
3. Run `shunkan connect zerodha` and paste the api_key and api_secret once.

Kite invalidates the access token every morning around 07:30 IST, so
`shunkan connect zerodha` is a daily ritual. Zerodha's terms say automating that
login isn't allowed, so Shunkan doesn't try.

Credentials go to `~/.shunkan/credentials.json` with mode 0600. Nothing is sent
anywhere except Kite.

### Without a broker

Most of it still works. The NFO instruments dump is served unauthenticated, so
correct lot sizes and expiry ladders are available to everyone. Chains fall back
to NSE's public API, which is free, roughly a minute delayed, and bot blocked on
some networks. When it's blocked you get the refusal card, which is the point.

`SHUNKAN_OFFLINE=1` gives you a fully synthetic demo. Everything is labelled
`MODELLED` in amber, ΔOI is withheld, and the store refuses to persist any of it.

## What's in it

**OPT, the option chain.** The screen the product exists for. Live chain off
Kite or NSE, correct contract lot, an expiry selector, ATM held in view, OI
build-up measured against a real previous-session basis, and a source badge
saying where every number came from. It repaints in place rather than
rebuilding, so it never blanks and never loses your scroll position.

**Order ticket.** Click a premium in the chain. Opens on SELL because that's the
default intent on an option, `S`/`B` flip side, digits set lots, Enter books, Esc
closes. It books at the premium you clicked, never a market order.

**PRT, the book.** Net delta, gamma, theta and vega across every position, plus
a per-underlying breakdown. A short straddle shows up as delta flat, short gamma,
earning theta, short vega, which is what you actually need to see. Exchange
priced margin via Kite's SPAN calculator, because an iron condor's real margin is
about a third of a naked short's and no local approximation gets close.

**Vol surface calibration.** SABR fitted to the smile a live chain is actually
quoting, via Hagan's closed form, about 2.5ms per expiry. Fits alpha, rho and
nu by weighted least squares on the OTM wing of each strike, weighted by open
interest, because a vol solved off a contract nobody holds is a number rather
than a quote. beta is an input, not a fitted output: beta and rho are close to
degenerate on a single smile, so fitting both finds noise and reports it as
structure.

Every fit carries its residuals and a quality grade. A smile the model cannot
represent shows up as "poor" with visible errors rather than as a smooth curve,
and calibrating a modelled chain is refused outright, since that would recover
the generator's own parameters and render them as market structure.

**QNT, the quant lab.** WebGL surfaces for IV, Greeks, Monte Carlo, Heston,
correlation, VaR, the efficient frontier, Kalman, attention, and PSO
optimisation over real backtests.

Plus a chart with drawings and indicators, a payoff builder, IV rank and cones,
volume profile, a news feed with sentiment scoring, a screener, alerts, a
backtest lab with walk-forward and Monte Carlo, an ML studio, and a local data
store browser.

## Architecture

```
src/shunkan/
  data/         providers, NSE + Kite clients, chain resolution, caching
  derivatives/  chain model, Black-Scholes greeks, IV solving, payoffs
  portfolio/    instrument identity, sided ledger, book risk
  backtest/     engine, walk-forward, Monte Carlo, optimisation
  analytics/    indicators, stats, models, visualisation data
  store/        parquet archive for chains, history and instruments
  server/       FastAPI app and the web terminal
  ui/           Textual TUI (the original interface, still works)
```

A few decisions worth knowing if you're reading the code:

**Positions are keyed by venue.** `NFO:NIFTY|2026-08-18|24500|CE`, not `NIFTY`.
SENSEX options list on BFO while NIFTY's are on NFO, so without the venue in the
key a book long one and short the other would net to flat. Same underlying,
different contract.

**Quantities are in units, not lots.** Lots are the trading unit and get
converted at the boundary. Storing lots would silently produce nonsense the day
NSE revises a lot size, which they do.

**Every lot records the side it opened on.** A sell first offsets opposing lots
FIFO, then opens whatever's left short. That's how a broker's book behaves, and
it means "sell 2 while long 1" needs no intent flag from the caller.

**Time to expiry is seconds to 15:30 IST**, not whole days. On expiry day the
difference is the whole position: a flat 12 hours versus the 40 minutes you
actually have left.

**The instruments dump gets archived daily.** Exchanges flush F&O instrument
tokens at every expiry and Kite can't serve expired ones afterwards. A day you
didn't archive is options history that nothing can reconstruct later. It's about
3MB a day and it's the only path to a self owned options history.

## Data and storage

Everything lives under `~/.shunkan`:

```
credentials.json          broker tokens, mode 0600
store/history/            daily OHLCV per symbol, parquet
store/chains/             option chain snapshots, real sources only
store/instruments/        daily contract master per venue
cache/                    instruments dump, short lived
```

The chain store refuses to persist a modelled chain. That's enforced on an
`is_model` flag rather than by string matching a source name, because
relabelling a synthetic chain used to walk straight past the old check.

## Tests

```
pytest tests -q
```

342 tests. Worth reading if you want the invariants: a good chunk of them exist
specifically to pin down honesty properties, like
`test_store_refuses_model_chain_with_real_source` or
`test_margin_goes_unknown_when_only_the_SIZE_changes`.

## Terms, and please read this one

Zerodha's Kite Connect terms prohibit displaying live market data "to the public
at large" and forbid building redistributable caches of it. Running
`shunkan serve` bound to localhost for yourself is fine. Putting it on a shared
host, a VPS with a public port, or in front of other users is a licence
violation and can get your API access terminated.

Shunkan has no accounts. `shunkan serve` refuses to bind a non-loopback
address unless you pass `--i-understand-the-risk`, and when you do it prints a
per-run token that every API call and the tick socket require. Treat that as a
guard rail rather than a security model: it is one shared secret, the licence
problem above does not go away, and you are still exposing a live broker
session.

## Status

Actively being built. The option chain and the position book are the parts that
have had the most work and the most tests. Known rough edges are tracked in
[docs/STATUS.md](docs/STATUS.md), which is kept honest on purpose.

## Disclaimers

Paper trading only. Shunkan never places a real order and there's no code path
that could. The book is a local ledger.

Nothing here is investment advice. Backtest results aren't predictions, and the
Monte Carlo and walk-forward tooling exists specifically to make you more
sceptical of your own results, not less.

## Licence

MIT. See [LICENSE](LICENSE).
