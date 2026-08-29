"""Shunkan engine server — REST + WebSocket over the same engines the TUI uses.

Run with `shunkan serve`. Sync engine calls run in FastAPI's threadpool
(endpoints are plain `def`), so a slow Yahoo fetch never blocks the tick
WebSocket. Quotes/chains ride the engines' TTL caches; the browser polls
cheap endpoints frequently and gets pushed ticks over /ws/ticks.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from shunkan import __version__
from shunkan.alerts import AlertBook, desktop_notify, parse_alert
from shunkan.config import load_watchlist, save_watchlist
from shunkan.data.contract_specs import (ORDER_IN_LOTS_VENUES,
                                          economic_lot_size)
from shunkan.data.provider import DataError, get_provider, is_offline
from shunkan.markets import (
    GLOBAL_PULSE,
    INDIA_PULSE,
    IST,
    denormalize_symbol,
    session_phase,
)
from shunkan.portfolio import Portfolio
from shunkan.provenance import prov

STATIC_DIR = Path(__file__).parent / "static"


def _clean(obj):
    """JSON-safe: numpy scalars -> python, NaN/inf -> None, arrays -> lists."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_clean(v) for v in obj.tolist()]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        obj = float(obj)
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _quote_dict(q) -> dict:
    return _clean(
        {
            "symbol": q.symbol, "price": q.price, "change": q.change,
            "change_pct": q.change_pct, "volume": q.volume,
            "prev_close": q.prev_close, "day_high": q.day_high,
            "day_low": q.day_low, "market_cap": q.market_cap, "name": q.name,
            # What the terminal should route on when this row is clicked. The
            # frontend used to derive it by truncating `name` at the first
            # space, which sent BANK NIFTY to "BANK" and S&P 500 to "S&P":
            # seven of the nine rows on the landing board went nowhere.
            "chart_symbol": denormalize_symbol(q.symbol),
        }
    )


def _same_secret(sent: str, expected: str) -> bool:
    """Constant-time compare. encode() first: compare_digest raises
    UnicodeEncodeError on a non-ASCII str, which would turn a junk header into
    an unhandled 500 instead of a clean 401."""
    import hmac

    return hmac.compare_digest(sent.encode("utf-8", "replace"),
                               expected.encode("utf-8", "replace"))


# 60s through the session gives ~375 samples a day against ~6 at the old 600s.
# Well inside Kite's limits: a chain build is one batched /quote plus a cached
# instruments read, so two symbols cost ~4 calls a minute against a 1 req/s
# quote budget.
CAPTURE_INTERVAL_S = 60.0
CAPTURE_SYMBOLS = ("NIFTY", "BANKNIFTY")

# Live counters for the capture loop, surfaced on /api/status. Not persisted:
# the question this answers is "is the archive growing right now", and a
# restart is exactly when you want the count to start again.
capture_status: dict = {"ok": 0, "failed": 0, "skipped": 0}

# Six hours: the harvest is an idempotent upsert over data that changes once a
# day, and a full sweep is ~15 min of rate-limited calls. Four passes a day is
# ample to catch every expiry before it delists, and re-running is free.
HARVEST_INTERVAL_S = 6 * 3600.0
harvest_status: dict = {"runs": 0, "failed": 0}
participant_status: dict = {"failed": 0}
news_status: dict = {"failed": 0}
journal_status: dict = {"written": 0}
graph_status: dict = {"failed": 0}
keepalive_status: dict = {"active": False}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _chain_error(symbol: str, exc: Exception) -> HTTPException:
    """502 whose detail carries the source trail, so a failed chain shows
    which sources were tried and why — instead of a stand-in book."""
    return HTTPException(502, {
        "error": str(exc),
        "symbol": symbol.upper(),
        "source_trail": list(getattr(exc, "source_trail", [])),
        "offline": is_offline(),
    })


def _warm_delta_oi_basis(chain) -> None:
    """Seed this series' ΔOI basis from settled exchange history, off-thread.

    Fire-and-forget by design: the chain must render now with an honest "—"
    rather than block ~19s on a nicety. backfill_prev_close_oi is itself
    once-per-series, so repeated calls are cheap no-ops.
    """
    import threading

    def run():
        try:
            from shunkan.data.brokers import KiteProvider, get_broker
            from shunkan.data.kite_fno import backfill_prev_close_oi

            broker = get_broker()
            if isinstance(broker, KiteProvider):
                backfill_prev_close_oi(broker, chain)
        except Exception:
            pass  # a missing buildup column must never break the chain

    threading.Thread(target=run, daemon=True, name="delta-oi-warm").start()


class TradeRequest(BaseModel):
    """A fill. `symbol` alone means cash equity; adding expiry/strike/kind
    names a derivative contract, which is how the chain books a leg.

    `lots` is the unit F&O is actually quoted in and is preferred over
    `quantity` — but it is only honoured when the lot size is known, because
    a guessed lot would silently misstate the whole position.
    """

    side: str
    symbol: str
    quantity: float | None = None
    lots: int | None = None
    price: float | None = None
    kind: str = "EQ"                 # EQ | FUT | CE | PE
    expiry: str | None = None        # YYYY-MM-DD
    strike: float | None = None
    exchange: str | None = None      # defaults to the usual venue for the kind
    lot_size: int | None = None


class BrokerSetup(BaseModel):
    # Must live at module scope: `from __future__ import annotations` makes
    # `req: BrokerSetup` a string that FastAPI resolves against module globals,
    # so a class defined inside create_app() is invisible and the request body
    # degrades into a query parameter with a 422 that names the wrong field.
    api_key: str
    api_secret: str


class SettleRequest(BaseModel):
    """A settlement recorded against a contract that has stopped trading.

    `symbol` is the position key exactly as /api/portfolio reports it
    ("NFO:NIFTY|2026-08-11|24500|CE"), so venue, series and strike come from
    the book rather than being re-parsed off a form and possibly disagreeing
    with it. `price` is per unit and has no default: an expiry that settled
    worthless is a real zero the trader asserts, and a defaulted zero would be
    the terminal quietly filling in the most common answer.
    """

    symbol: str
    price: float


class BacktestRequest(BaseModel):
    symbol: str
    strategy: str = "sma_cross"
    params: dict[str, float] = {}
    period: str = "5y"
    mode: str = "backtest"  # backtest | walkforward | montecarlo | validate


class BuilderRequest(BaseModel):
    symbol: str
    interval: str = "1d"
    period: str = "1y"
    spec: dict  # RuleSpec.from_dict shape
    initial_cash: float = 10_000.0
    commission: float = 0.0005
    slippage: float = 0.0005
    sl_mode: str = "none"
    sl_value: float = 0.0
    tp_mode: str = "none"
    tp_value: float = 0.0
    trailing: bool = False
    atr_period: int = 14
    session_start: str | None = None
    session_end: str | None = None
    cooldown_bars: int = 0
    atr_min: float | None = None
    atr_max: float | None = None
    allow_short: bool = True


class AlertRequest(BaseModel):
    rule: str


class SwarmRequest(BaseModel):
    symbol: str
    strategy: str = "sma_cross"
    period: str = "5y"
    particles: int = 24
    iters: int = 30
    start: str | None = None
    end: str | None = None


class ScriptRequest(BaseModel):
    symbol: str
    code: str
    period: str = "2y"
    interval: str = "1d"


class MLTrainRequest(BaseModel):
    symbol: str
    features: list[str]
    model: str = "stumps"
    period: str = "5y"
    horizon: int = 5
    test_split: float = 0.25
    start: str | None = None
    end: str | None = None


def create_app(access_token: str = "", allowed_hosts: tuple[str, ...] = ()) -> FastAPI:
    """Build the app.

    `access_token` and `allowed_hosts` are set by `shunkan serve` when it binds
    to anything other than loopback. Left empty (the default, and the only case
    for a normal localhost run) the guards below are inert, so nothing about
    the single-user experience changes.
    """
    provider = get_provider()
    portfolio = Portfolio.load()
    alert_book = AlertBook()
    hub = TickHub()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.loop = asyncio.get_running_loop()

        async def alert_loop():
            while True:
                await asyncio.sleep(60.0)
                if not alert_book.armed:
                    continue
                try:
                    fired = await asyncio.to_thread(alert_book.check_all, provider)
                except Exception:
                    continue
                for alert, current in fired:
                    msg = f"ALERT {alert.describe().split(' — ')[0]} — now {current:,.2f}"
                    desktop_notify("Shunkan alert", msg)
                    await hub.broadcast({"type": "alert", "message": msg})

        async def bar_flush_loop():
            """Persist completed 1-minute bars from the live tick stream."""
            from shunkan.store import TickStore

            tick_store = TickStore()
            while True:
                await asyncio.sleep(30.0)
                bars = hub.bars.drain()
                if bars:
                    try:
                        await asyncio.to_thread(tick_store.write_bars, bars)
                    except Exception:
                        pass

        async def chain_capture_loop():
            """Snapshot index chains through the session so IV, skew and OI
            series accumulate whether or not anyone is watching.

            This archive is the only thing that compounds. Expired option
            contracts cannot be re-fetched from Kite afterwards, so a session
            not captured is a session gone for good, and there is no amount of
            later effort that recovers it.

            It used to sleep 600s and swallow every failure with a bare
            `except: continue`. Kite invalidates its token every morning and
            requires a manual login, so from 09:15 until someone logged in this
            captured nothing and said nothing. Measured: 2026-08-17 had ONE
            snapshot for the whole session, 2026-08-14 had 13. That is not a
            dataset, and the silence is what made it invisible.
            """
            from shunkan.data.chains import get_chain

            await asyncio.sleep(20.0)  # let startup settle before the first pull
            while True:
                if is_offline() or not session_phase().is_open:
                    capture_status["skipped"] = capture_status.get("skipped", 0) + 1
                    capture_status["last_skip_reason"] = (
                        "offline" if is_offline() else session_phase().phase)
                    await asyncio.sleep(CAPTURE_INTERVAL_S)
                    continue
                for sym in CAPTURE_SYMBOLS:
                    try:
                        c = await asyncio.to_thread(get_chain, sym)
                        capture_status["ok"] = capture_status.get("ok", 0) + 1
                        capture_status["last_ok"] = _now_iso()
                        capture_status["last_source"] = c.source
                        capture_status.setdefault("per_symbol", {})[sym] = {
                            "at": _now_iso(), "source": c.source,
                            "expiry": str(c.expiry),
                        }
                    except Exception as exc:
                        # Counted and named. A capture that fails all morning is
                        # the single most expensive silent failure in this app.
                        capture_status["failed"] = capture_status.get("failed", 0) + 1
                        capture_status["last_error"] = f"{sym}: {str(exc)[:160]}"
                        capture_status["last_error_at"] = _now_iso()
                await asyncio.sleep(CAPTURE_INTERVAL_S)

        async def history_sync_loop():
            """Grow the local daily-candle archive while the terminal runs:
            pulse boards + watchlist, upserted on start and every 6 h. Never
            runs offline — synthetic data is never written to the store."""
            from shunkan.store import HistoryArchive

            archive = HistoryArchive()
            await asyncio.sleep(25.0)  # let startup traffic settle first
            while True:
                if not is_offline():
                    # Pulse boards + watchlist + the FULL constituent
                    # universe. It used to stop at the watchlist, so 50-odd
                    # constituents' archives froze at their seed date and the
                    # signals scan was reading week-old candles for most of
                    # the universe (the per-row dates said so - that is what
                    # they are for).
                    try:
                        from shunkan.data.constituents import universe as _uni

                        constituent_syms = [f"{c.symbol}.NS" for c in _uni()]
                    except Exception:
                        constituent_syms = []
                    symbols = list(dict.fromkeys(
                        [r["ticker"] for r in (_pulse_boards()["india"]
                                               + _pulse_boards()["global"])]
                        + load_watchlist() + constituent_syms))
                    src = (getattr(provider, "broker_name", "") or "yahoo/nse")

                    def _sync_all():
                        n = 0
                        for s in symbols:
                            try:
                                h = provider.history(s, period="1y", interval="1d")
                                archive.upsert(s, h, src)
                                n += 1
                            except Exception:
                                continue
                        return n

                    try:
                        await asyncio.to_thread(_sync_all)
                    except Exception:
                        pass
                await asyncio.sleep(6 * 3600.0)

        async def harvest_loop():
            """Pull the full traded life of every listed option contract.

            The only job here with a real deadline. Kite deletes an expired
            contract from the instruments master, so after expiry its candles
            cannot be fetched and its token cannot even be named. A contract
            still listed carries months of history; measured 2026-08-17, the
            series expiring the next day held 24 day-candles back to 2026-07-15
            and the full sweep returned 133,968 candles back to 2025-07-13.

            Runs on a long cycle because it is an upsert over a slow-moving
            asset: a full sweep is ~15 minutes of rate-limited calls and the
            data only changes once a day. Expiries closest to expiring are
            swept first, so an interrupted run has already saved the part that
            disappears soonest.
            """
            from shunkan.data.brokers import KiteProvider, get_broker
            from shunkan.data.harvest import harvest_contract_lives, settling_today
            from shunkan.markets import is_expired, now_ist

            await asyncio.sleep(120.0)  # never compete with the open
            while True:
                if not is_offline():
                    try:
                        broker = get_broker()
                        if isinstance(broker, KiteProvider):
                            # An expiry settling today is only fetchable between
                            # the close and Kite dropping it from the master
                            # overnight. That final session is the one every
                            # expiry question needs, and the archive currently
                            # holds ZERO settlements because this window was
                            # never targeted. It goes first, alone, so a slow
                            # full sweep cannot eat the window.
                            for exp in await asyncio.to_thread(settling_today, broker):
                                if is_expired(exp):
                                    r = await asyncio.to_thread(
                                        harvest_contract_lives, broker,
                                        ("NIFTY", "BANKNIFTY"), "day", exp)
                                    harvest_status["settlements"] = (
                                        harvest_status.get("settlements", 0)
                                        + sum(x.contracts_written for x in r))
                                    harvest_status["last_settlement"] = (
                                        f"{exp} at {now_ist():%H:%M}")
                            res = await asyncio.to_thread(
                                harvest_contract_lives, broker)
                            harvest_status["runs"] = harvest_status.get("runs", 0) + 1
                            harvest_status["last_ok"] = _now_iso()
                            harvest_status["candles"] = sum(
                                r.candles_written for r in res)
                            harvest_status["detail"] = [r.summary() for r in res]
                        else:
                            harvest_status["last_skip"] = "no Kite session"
                    except Exception as exc:
                        # Named, not swallowed. A harvest that silently stops is
                        # how a week of expiries goes missing unnoticed.
                        harvest_status["failed"] = harvest_status.get("failed", 0) + 1
                        harvest_status["last_error"] = str(exc)[:200]
                        harvest_status["last_error_at"] = _now_iso()
                await asyncio.sleep(HARVEST_INTERVAL_S)

        async def feed_keepalive_loop():
            """Hold the tick feed open through market hours regardless of
            websocket clients, so the bar archive records the session and
            not just the stretches somebody happened to be watching."""
            from shunkan.data.brokers import KiteProvider, get_broker

            await asyncio.sleep(30.0)
            while True:
                try:
                    live_possible = False
                    if not is_offline() and session_phase().is_open:
                        try:
                            live_possible = isinstance(get_broker(), KiteProvider)
                        except DataError:
                            live_possible = False
                    if live_possible:
                        hub.keepalive = True
                        keepalive_status["active"] = True
                        await hub.ensure_feed()
                        # Self-healing: a startup race can build the feed
                        # without its front futures; repair every cycle and
                        # say the outcome where /api/status can see it.
                        try:
                            from shunkan.stream.factory import ensure_front_futures

                            labels = await asyncio.to_thread(
                                ensure_front_futures, hub.feed)
                            for lab in labels:
                                if lab not in hub._base:
                                    hub._base.append(lab)
                            keepalive_status["futures"] = ",".join(labels) or "none"
                        except Exception as exc:
                            keepalive_status["futures"] = f"repair failed: {str(exc)[:120]}"
                    else:
                        hub.keepalive = False
                        keepalive_status["active"] = False
                        if not hub._senders and hub.feed is not None:
                            hub.stop()
                except Exception as exc:
                    keepalive_status["last_error"] = str(exc)[:160]
                await asyncio.sleep(60.0)

        async def graph_loop():
            """Keep the knowledge graph current with the parquet stores.

            Cheap (seconds) and idempotent, so it runs on a slow cycle and
            after startup - the terminal should never open a company page
            against a graph that predates this morning's scans."""
            from shunkan.data.ingest import rebuild

            await asyncio.sleep(45.0)
            while True:
                try:
                    r = await asyncio.to_thread(rebuild)
                    graph_status.update({k: v for k, v in r.items() if k != "stats"})
                    graph_status["nodes"] = r["stats"]["nodes"]
                    graph_status["edges"] = r["stats"]["edges"]
                    graph_status["last_ok"] = _now_iso()
                except Exception as exc:
                    graph_status["failed"] = graph_status.get("failed", 0) + 1
                    graph_status["last_error"] = str(exc)[:160]
                await asyncio.sleep(6 * 3600.0)

        async def analysis_journal_loop():
            """Record what the terminal said at each close, once per day.

            The journal is what makes "did yesterday's analysis hold up" a
            query instead of an archaeology project: replay serves the
            recorded JSON, not a recomputation through code that has since
            changed. Waits for the history sync to roll today's candle so a
            journal never records yesterday wearing today's date."""
            from shunkan.analytics.daily import journal_path, write_journal
            from shunkan.markets import now_ist

            await asyncio.sleep(120.0)
            while True:
                try:
                    now = now_ist()
                    after_close = now.hour * 60 + now.minute >= 15 * 60 + 40
                    if not is_offline() and now.weekday() < 5 and after_close:
                        for jsym in ("NIFTY", "BANKNIFTY"):
                            if journal_path(jsym, now.date()).exists():
                                continue
                            payload = await asyncio.to_thread(_compose_daily, jsym, None)
                            if payload.get("as_of") != now.date().isoformat():
                                continue    # today's candle not rolled yet; retry
                            if write_journal(jsym, now.date(), _clean(payload)):
                                journal_status["written"] = journal_status.get("written", 0) + 1
                                journal_status[jsym] = now.date().isoformat()
                                journal_status["last_ok"] = _now_iso()
                except Exception as exc:
                    journal_status["last_error"] = str(exc)[:160]
                await asyncio.sleep(600.0)

        async def news_archive_loop():
            """Persist headlines so future event studies have something to
            join a move against.

            Two strands per cycle: the general market feed (the unbiased
            channel), and a rotating slice of constituent-name queries so
            per-company depth accumulates without hammering Google. The full
            NIFTY50+BANKNIFTY rotation completes in about four hours at six
            names per half-hour cycle.
            """
            from shunkan.data.constituents import alias_table, universe
            from shunkan.data.newsstore import backfill_symbol, persist
            from shunkan.intel.feeds import fetch_news

            rotation = 0
            await asyncio.sleep(60.0)
            while True:
                if not is_offline():
                    try:
                        uni = await asyncio.to_thread(universe)
                        aliases = alias_table(uni)
                        items = await asyncio.to_thread(fetch_news, None, 40)
                        n = await asyncio.to_thread(
                            persist, items, "live", aliases)
                        for c in uni[rotation % len(uni):][:6]:
                            r = await asyncio.to_thread(
                                backfill_symbol, c.symbol, c.name, 1,
                                None, aliases, None, None, "rotation")
                            n += r["added"]
                        rotation += 6
                        news_status["cycles"] = news_status.get("cycles", 0) + 1
                        news_status["added_last"] = n
                        news_status["last_ok"] = _now_iso()
                    except Exception as exc:
                        news_status["failed"] = news_status.get("failed", 0) + 1
                        news_status["last_error"] = str(exc)[:160]
                await asyncio.sleep(1800.0)

        async def participant_loop():
            """Keep the NSE participant-wise positioning current.

            The file for a session appears in the evening; a 6-hourly
            backfill(days=7) picks it up whenever it lands, refetches nothing
            already on disk, and quietly rides over weekends and holidays.
            This is the who-moved table the daily analysis reads, and unlike
            option candles it is backfillable, so a missed evening costs
            latency rather than data.
            """
            from shunkan.data.participant import backfill

            await asyncio.sleep(90.0)
            while True:
                if not is_offline():
                    try:
                        from shunkan.data.participant import store_path

                        # A shallow store deep-backfills itself once: the
                        # archive serves years and the 4-year research pull
                        # proved zero failed fetches, so every deployment
                        # should inherit that depth automatically. The loop
                        # is the store's only writer, which is what makes
                        # this safe to do here and nowhere else.
                        days = 7
                        try:
                            have = len(pd.read_parquet(
                                store_path(), columns=["date"])["date"].unique())
                        except Exception:
                            have = 0
                        if have < 400:
                            days = 1500
                            participant_status["deep_backfill"] = "running"
                        r = await asyncio.to_thread(backfill, days)
                        participant_status.update(r)
                        participant_status["last_ok"] = _now_iso()
                        if days == 1500:
                            participant_status["deep_backfill"] = "done"
                    except Exception as exc:
                        participant_status["failed"] = (
                            participant_status.get("failed", 0) + 1)
                        participant_status["last_error"] = str(exc)[:160]
                await asyncio.sleep(6 * 3600.0)

        async def instruments_archive_loop():
            """Keep each day's contract master.

            Exchanges flush F&O instrument_tokens at every expiry and Kite
            cannot serve expired ones afterwards, so a day not archived is a
            day of options history nothing can reconstruct later. ~3 MB, no
            credentials needed — the cheapest irreversible asset here.
            """
            from shunkan.data.kite_fno import archive_instruments_dump

            await asyncio.sleep(40.0)
            while True:
                if not is_offline():
                    # Every venue a desk trades: NSE/BSE cash, NFO and BFO index
                    # and stock derivatives, MCX commodities, CDS currency.
                    for exchange in ("NFO", "BFO", "MCX", "CDS", "NSE", "BSE"):
                        try:
                            await asyncio.to_thread(archive_instruments_dump, exchange)
                        except Exception:
                            pass  # never let an archive miss break startup
                await asyncio.sleep(6 * 3600.0)

        tasks = [asyncio.create_task(t()) for t in
                 (alert_loop, bar_flush_loop, chain_capture_loop, history_sync_loop,
                  instruments_archive_loop, harvest_loop, participant_loop,
                  news_archive_loop, feed_keepalive_loop, analysis_journal_loop,
                  graph_loop)]
        yield
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        hub.stop()

    app = FastAPI(title="Shunkan", version=__version__, lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1500)

    @app.middleware("http")
    async def _guard(request, call_next):
        """Host pinning, token auth and an Origin check on writes.

        All three are no-ops on a default localhost run. They exist because
        this process holds a live broker session: without them, binding to a
        LAN address hands the position book, the trade endpoint and the
        licensed Kite tape to the whole network segment.

        The Origin check covers the loopback case too. A page that has DNS
        rebound to 127.0.0.1 is same-origin, so CORS never fires, and the
        bodyless POSTs here are CORS-simple and would never see a preflight.
        """
        if allowed_hosts:
            host = (request.headers.get("host") or "").split(":")[0]
            if host not in allowed_hosts:
                return JSONResponse({"detail": "host not allowed"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "cross-origin write refused"},
                                    status_code=403)
        if access_token and request.url.path.startswith("/api/"):
            sent = (request.headers.get("x-shunkan-token")
                    or request.query_params.get("t") or "")
            if not _same_secret(sent, access_token):
                return JSONResponse({"detail": "unauthorised"}, status_code=401)
        return await call_next(request)

    @app.middleware("http")
    async def _no_stale_static(request, call_next):
        """Static assets revalidate on every load (cheap ETag 304s) so a
        terminal upgrade never leaves a browser running last week's app.js."""
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static"):
            resp.headers.setdefault("Cache-Control", "no-cache")
        return resp

    # -- status / pulse ----------------------------------------------------

    _health_cache = {"at": 0.0, "healthy": None, "reason": "not probed"}

    def _broker_health(force: bool = False) -> dict:
        """Probe REST health (60s cache). 'Configured' green is a lie when
        the daily token is dead — the chip colors from THIS, not from config."""
        if is_offline():
            return {"healthy": None, "reason": "offline mode"}
        if not force and time.time() - _health_cache["at"] < 60:
            return {"healthy": _health_cache["healthy"],
                    "reason": _health_cache["reason"]}
        healthy, reason = None, "no broker configured"
        try:
            from shunkan.data.brokers import KiteProvider, get_broker

            b = get_broker()
            if isinstance(b, KiteProvider):
                healthy, reason = b.healthy()
        except Exception as exc:
            healthy, reason = False, str(exc)[:120]
        _health_cache.update(at=time.time(), healthy=healthy, reason=reason)
        return {"healthy": healthy, "reason": reason}

    @app.get("/api/broker/status")
    def broker_status():
        return _broker_health()

    @app.post("/api/broker/setup")
    def broker_setup(req: BrokerSetup):
        """One-time credential entry, so the terminal is self-contained.

        This was CLI-only, which meant a fresh install (or a fresh container)
        could not be brought up from the app it ships with. The secret is
        written 0600 and is never returned, logged or echoed into an error:
        the only response is whether it worked.
        """
        from shunkan.data.brokers import save_credentials

        if is_offline():
            raise HTTPException(400, "offline mode — nothing to connect to")
        key = (req.api_key or "").strip()
        secret = (req.api_secret or "").strip()
        # Shape check only. Kite does not publish exact lengths, so anything
        # stricter would reject valid credentials on a guess.
        if not key or not secret:
            raise HTTPException(400, "both api_key and api_secret are required")
        if len(key) < 6 or len(secret) < 6 or any(c.isspace() for c in key + secret):
            raise HTTPException(400, "that does not look like a Kite api_key/api_secret "
                                     "— check for a stray space or a truncated paste")
        save_credentials("zerodha", api_key=key, api_secret=secret)
        # Deliberately returns nothing about the values themselves.
        return {"ok": True, "next": "login"}

    @app.post("/api/broker/reconnect")
    async def broker_reconnect(request: Request):
        """Web-native daily re-auth: returns the Zerodha login URL for the
        browser to open (credentials are typed on Zerodha's page, never
        here), while a background task catches the redirect on :8722,
        exchanges the token, and hot-swaps it into the live session."""
        from shunkan.data.brokers import (
            KiteProvider, get_broker, kite_catch_and_exchange, kite_login_url,
            load_credentials,
        )

        if is_offline():
            raise HTTPException(400, "offline mode")
        creds = load_credentials().get("zerodha", {})
        api_key, api_secret = creds.get("api_key"), creds.get("api_secret")
        if not (api_key and api_secret):
            raise HTTPException(400, "No saved Zerodha api_key/api_secret — "
                                     "add them from the broker chip in the top bar")

        # Send the browser back to the page it started from. Taken from the
        # request's own origin rather than a config value, so it is right
        # whatever port or host the terminal is actually being reached on.
        # _safe_return_to rejects anything non-loopback: the callback page
        # carries a request_token in its URL, so an open redirect there would
        # hand that token to whoever asked.
        origin = request.headers.get("origin") or str(request.base_url)

        async def catch():
            try:
                token = await asyncio.to_thread(
                    kite_catch_and_exchange, api_key, api_secret,
                    return_to=origin)
                b = get_broker()
                if isinstance(b, KiteProvider):
                    b.set_token(api_key, token)
                _broker_health(force=True)
                await hub.broadcast({"type": "alert",
                                     "message": "Kite reconnected — live REST restored"})
            except Exception:
                pass

        asyncio.create_task(catch())
        return {"login_url": kite_login_url(api_key),
                "note": "complete the Zerodha login in the opened tab; "
                        "the terminal captures the token automatically"}

    @app.get("/api/status")
    def status():
        phase = session_phase()
        broker = None
        if not is_offline():
            try:
                from shunkan.data.brokers import get_broker

                b = get_broker()
                broker = type(b).__name__.removesuffix("Provider") if b else None
            except Exception:
                broker = None
        health = _broker_health()
        return {
            "version": __version__,
            "offline": is_offline(),
            "broker": broker,
            "broker_healthy": health["healthy"],
            "broker_reason": health["reason"],
            "session": {"phase": phase.phase, "open": phase.is_open,
                        "description": phase.description},
            # Is the archive actually growing? Expired contracts cannot be
            # re-fetched, so a morning of silent capture failures is
            # unrecoverable and has to be visible while it is happening.
            "capture": dict(capture_status),
            # Option history is the only irreversible asset here: an expiry not
            # harvested before it delists cannot be bought back at any price.
            "harvest": dict(harvest_status),
            "participant": dict(participant_status),
            "news_archive": dict(news_status),
            # Who is streaming what, and whether backpressure is eating
            # frames (drops are counted, never silent).
            "ticks": hub.stats(),
            "feed_keepalive": dict(keepalive_status),
            "analysis_journal": dict(journal_status),
            "graph": dict(graph_status),
            "server_time": datetime.now(timezone.utc).isoformat(),
        }

    def _pulse_boards() -> dict:
        """The two pulse boards, user-editable. Falls back to the built-in
        lists; a saved file wins. Entries are {name, ticker}."""
        from shunkan.config import APP_DIR

        path = APP_DIR / "pulse_boards.json"
        if path.exists():
            try:
                saved = json.loads(path.read_text())
                if saved.get("india") and saved.get("global"):
                    return saved
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "india": [{"name": n, "ticker": t} for n, t in INDIA_PULSE],
            "global": [{"name": n, "ticker": t} for n, t in GLOBAL_PULSE],
        }

    @app.get("/api/pulse/boards")
    def get_pulse_boards():
        return _pulse_boards()

    @app.post("/api/pulse/boards")
    def save_pulse_boards(body: dict):
        """Persist edited boards. Refuses empty boards and junk shapes -
        a pulse with nothing on it is a mistake, not a preference."""
        from shunkan.config import APP_DIR, ensure_dirs

        out = {}
        for key in ("india", "global"):
            rows = body.get(key) or []
            clean = []
            for r in rows:
                t = str(r.get("ticker", "")).strip()
                if not t or len(t) > 24:
                    continue
                clean.append({"name": (str(r.get("name", "")).strip() or t)[:28],
                              "ticker": t})
            if not clean or len(clean) > 20:
                raise HTTPException(400, f"{key}: between 1 and 20 instruments")
            out[key] = clean
        ensure_dirs()
        (APP_DIR / "pulse_boards.json").write_text(json.dumps(out, indent=2))
        return {"ok": True}

    @app.get("/api/pulse")
    def pulse(cached: int = 0):
        """The landing board. ?cached=1 returns the last REAL snapshot from
        disk instantly, stamped with the time it was true, so a cold start
        paints in milliseconds with an honest AS OF instead of sitting on
        spinners for the ~20s the live quote fan-out takes. The frontend then
        fetches live and repaints. Offline mode neither writes nor serves the
        snapshot: a synthetic board must never be persisted as a real one.
        """
        from shunkan.config import CACHE_DIR

        snap_path = CACHE_DIR / "pulse_snapshot.json"
        if cached:
            if is_offline() or not snap_path.exists():
                raise HTTPException(404, "no snapshot yet")
            try:
                return json.loads(snap_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise HTTPException(404, "snapshot unreadable") from exc

        boards = {}
        boards_cfg = _pulse_boards()
        for key, board in (("india", [(r["name"], r["ticker"]) for r in boards_cfg["india"]]),
                           ("global", [(r["name"], r["ticker"]) for r in boards_cfg["global"]])):
            try:
                quotes = provider.quotes([t for _, t in board])
            except DataError:
                quotes = {}
            boards[key] = [
                {**(_quote_dict(quotes[t.upper()]) if t.upper() in quotes else {"symbol": t}),
                 "name": name}  # board label wins over the quote's ticker name
                for name, t in board
            ]
        got_any = any("price" in row for b in boards.values() for row in b)
        boards["as_of"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if got_any and not is_offline():
            try:
                snap_path.parent.mkdir(parents=True, exist_ok=True)
                snap_path.write_text(json.dumps(boards))
            except OSError:
                pass  # a failed snapshot write must never break the live board
        return boards

    @app.get("/api/sparks")
    def sparks(symbols: str, period: str = "1mo"):
        """Mini close-series for sparklines: {SYM: [closes…]} (rides the
        provider's on-disk cache, so repeated pulse refreshes are free)."""
        out: dict[str, list[float]] = {}
        for sym in symbols.split(",")[:16]:
            sym = sym.strip()
            if not sym:
                continue
            try:
                df = provider.history(sym, period=period, interval="1d")
                out[sym.upper()] = [_clean(float(v)) for v in df["close"].tail(30)]
            except DataError:
                continue
        return out

    @app.get("/api/quotes")
    def quotes(symbols: str):
        syms = [s for s in symbols.split(",") if s.strip()]
        try:
            out = provider.quotes(syms)
        except DataError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {k: _quote_dict(v) for k, v in out.items()}

    @app.get("/api/history/{symbol}")
    def history(symbol: str, period: str = "6mo", interval: str = "1d"):
        try:
            df = provider.history(symbol, period=period, interval=interval)
        except DataError as exc:
            raise HTTPException(502, str(exc)) from exc
        ts = [int(t.timestamp()) for t in df.index]
        return {
            "symbol": symbol.upper(),
            "candles": [
                {"time": ts[i], "open": _clean(float(df['open'].iloc[i])),
                 "high": _clean(float(df['high'].iloc[i])),
                 "low": _clean(float(df['low'].iloc[i])),
                 "close": _clean(float(df['close'].iloc[i]))}
                for i in range(len(df))
            ],
            "volume": [
                {"time": ts[i], "value": _clean(float(df['volume'].iloc[i])),
                 "color": "rgba(63,185,80,0.45)" if df['close'].iloc[i] >= df['open'].iloc[i]
                 else "rgba(248,81,73,0.45)"}
                for i in range(len(df))
            ],
        }

    # -- charting ------------------------------------------------------------

    @app.get("/api/chart/catalog")
    def chart_catalog():
        """Indicator catalog powering the chart's Indicators menu."""
        from shunkan.server.charts import CHART_INDICATORS

        return {"indicators": CHART_INDICATORS}

    @app.get("/api/chart/indicators/{symbol}")
    def chart_indicators(symbol: str, period: str = "6mo", interval: str = "1d",
                         specs: str = ""):
        """Computed indicator series (overlays + oscillator panes) for a symbol,
        each carrying a provenance record. `specs` is e.g. 'sma:20,rsi:14,macd'."""
        from shunkan.server.charts import compute_indicator, parse_specs

        parsed = parse_specs(specs)
        if not parsed:
            return {"symbol": symbol.upper(), "indicators": []}
        try:
            df = provider.history(symbol, period=period, interval=interval)
        except DataError as exc:
            raise HTTPException(502, str(exc)) from exc
        times = [int(t.timestamp()) for t in df.index]
        source = "synthetic (offline)" if is_offline() else f"market data ({interval})"
        out = [compute_indicator(df, kind, p, times, source) for kind, p in parsed]
        return _clean({"symbol": symbol.upper(), "period": period,
                       "interval": interval, "indicators": out})

    @app.get("/api/chart/config/{symbol}")
    def get_chart_config(symbol: str):
        cfgs = _load_chart_configs()
        return cfgs.get(symbol.upper(), {})

    @app.post("/api/chart/config/{symbol}")
    def save_chart_config(symbol: str, body: dict):
        from shunkan.config import APP_DIR, ensure_dirs

        ensure_dirs()
        cfgs = _load_chart_configs()
        cfgs[symbol.upper()] = body
        (APP_DIR / "chart_configs.json").write_text(json.dumps(cfgs, indent=2))
        return {"ok": True}

    # -- derivatives ---------------------------------------------------------

    @app.get("/api/chain/{symbol}")
    def chain(symbol: str, expiry: str | None = None, strikes: int = 20):
        """`expiry` (YYYY-MM-DD) picks a listed expiry; `strikes` bounds the
        table to ATM±N rows. Analytics always run on the full chain."""
        from shunkan.data.chains import get_chain
        from shunkan.derivatives.chain import analyze_chain
        from shunkan.store import chain_delta_oi, straddle_series

        want = None
        if expiry:
            try:
                want = datetime.strptime(expiry, "%Y-%m-%d").date()
            except ValueError as exc:
                raise HTTPException(
                    400, f"expiry must be YYYY-MM-DD, got {expiry!r}") from exc
        try:
            # get_chain's TTL cache keys on the argument tuple, so the default
            # path must stay a one-arg call or it misses every other caller's
            # entry and re-fetches the same chain.
            c = get_chain(symbol, want) if want else get_chain(symbol)
            a = analyze_chain(c)
        except (DataError, ValueError) as exc:
            # A failed chain must say what it tried — the trail rides the error.
            raise _chain_error(symbol, exc) from exc

        # ΔOI: prefer locally stored snapshot basis (real, timestamped).
        # Without a basis the UI shows "—", never a fabricated zero.
        delta = None if c.is_model else chain_delta_oi(c)
        if delta is None and not c.is_model:
            # No basis for this series yet. The exchange's own settled day
            # candles have one; fetching it costs ~19s of rate-limited calls,
            # so it runs off the request path and lands on a later refresh.
            _warm_delta_oi_basis(c)
        has_native_delta = not c.is_model and bool(
            np.any(c.call_oi_change) or np.any(c.put_oi_change))
        rows = []
        for i in range(len(c.strikes)):
            d_call = d_put = None
            if delta is not None:
                d_call = _clean(float(delta["delta_call"][i]))
                d_put = _clean(float(delta["delta_put"][i]))
            elif has_native_delta:
                d_call = float(c.call_oi_change[i])
                d_put = float(c.put_oi_change[i])
            rows.append({
                "strike": float(c.strikes[i]),
                "call": {"ltp": float(c.call_ltp[i]), "oi": float(c.call_oi[i]),
                         "oi_change": d_call, "volume": float(c.call_volume[i]),
                         "iv": _clean(float(c.call_iv[i])),
                         "bid": _clean(float(c.call_bid[i])) if c.call_bid is not None else None},
                "put": {"ltp": float(c.put_ltp[i]), "oi": float(c.put_oi[i]),
                        "oi_change": d_put, "volume": float(c.put_volume[i]),
                        "iv": _clean(float(c.put_iv[i])),
                        "ask": _clean(float(c.put_ask[i])) if c.put_ask is not None else None},
                "atm": i == c.atm_index,
            })
        delta_basis = (
            "model chain — ΔOI not computed" if c.is_model
            else delta["basis"] if delta is not None
            else ("source-provided (NSE prev-day)" if has_native_delta
                  else "no stored snapshot yet — capturing from today")
        )

        # Display window: ATM±N strikes. NSE lists 100+ strikes per expiry and
        # the browser draws every one; the analytics above already ran on the
        # full chain, so this bounds the table only.
        rows_total = len(rows)
        if strikes > 0:
            atm_i = c.atm_index
            rows = rows[max(atm_i - strikes, 0):atm_i + strikes + 1]

        expiries = [str(e) for e in c.expiries] or [str(c.expiry)]
        call_total, put_total = float(c.call_oi.sum()), float(c.put_oi.sum())
        return _clean({
            "symbol": c.symbol, "spot": c.spot, "expiry": str(c.expiry),
            "expiries": expiries, "t_years": c.t_years,
            # When the source said this was true. null means it published no
            # time, and the UI then says "fetched" instead of "as of".
            "as_of": c.as_of.isoformat() if c.as_of else None,
            # lot_size is null when no source could name the contract lot —
            # the UI shows a dash and lot_size_source says why.
            "lot_size": c.lot_size, "lot_size_source": c.lot_size_source,
            "source": c.source,
            "is_model": c.is_model,
            "source_trail": c.source_trail, "rows": rows,
            "rows_total": rows_total, "strike_window": strikes,
            "delta_oi_basis": delta_basis,
            # snapshots are expiry-tagged; mixing expiries would fake the line
            "straddle_today": straddle_series(c.symbol, str(c.expiry)),
            "analytics": {
                "pcr_oi": a.pcr_oi, "pcr_volume": a.pcr_volume,
                "max_pain": a.max_pain, "support": a.support,
                "resistance": a.resistance, "atm_strike": a.atm_strike,
                "atm_iv": a.atm_iv, "straddle_price": a.straddle_price,
                "expected_move_pct": a.expected_move_pct,
                "bias": a.bias, "bias_reason": a.bias_reason,
                # synthetic.py plants the strikes this detector finds
                # (see its `hot` rows at lines 72-75) — on a model chain it
                # is pure fiction, so it never leaves the process.
                "unusual": [] if c.is_model else a.unusual,
            },
            "prov": {
                "pcr_oi": prov(
                    "PCR = Σ put OI / Σ call OI",
                    {"Σ put OI": (put_total, c.source), "Σ call OI": (call_total, c.source)},
                    c.source,
                    method="all strikes of the displayed expiry",
                ),
                "max_pain": prov(
                    "argmin over strikes K of Σ[call OI·max(K−strike,0)] + Σ[put OI·max(strike−K,0)]",
                    {"strikes": len(c.strikes), "expiry": str(c.expiry)},
                    c.source,
                    method="expiry level minimizing total option-buyer payoff",
                    caveat="a positioning magnet hypothesis, not a forecast",
                ),
                "expected_move_pct": prov(
                    "expected move = ATM straddle / spot",
                    {"ATM straddle": (a.straddle_price, c.source),
                     "spot": (c.spot, c.source),
                     "ATM strike": a.atm_strike},
                    c.source,
                    method="the market's own price for a move in either direction by expiry",
                ),
                "atm_iv": prov(
                    "mean(call IV, put IV) at ATM strike",
                    {"ATM strike": a.atm_strike,
                     "call IV": _clean(float(c.call_iv[c.atm_index])),
                     "put IV": _clean(float(c.put_iv[c.atm_index]))},
                    c.source,
                    method="IVs solved from option prices via Black-Scholes (Newton + bisection)"
                    if "Kite" in c.source else "IVs as provided by source",
                ),
                "bias": prov(
                    "score = PCR vote (>1.2 bullish, <0.8 bearish) + max-pain drift vote (>±0.5%)",
                    {"PCR": round(a.pcr_oi, 3),
                     "max pain drift": f"{(a.max_pain - c.spot) / c.spot:+.2%}"},
                    c.source,
                    method=a.bias_reason,
                    caveat="transparent rule-based read, not a trade signal",
                ),
                "delta_oi": prov(
                    "ΔOI[strike] = OI_now − OI_basis",
                    {"basis": delta_basis,
                     "basis snapshot": delta["basis_ts"] if delta else "—"},
                    "local chain store" if delta else c.source,
                ),
            },
        })

    @app.get("/api/payoff/{symbol}")
    def payoff(symbol: str, strategy: str | None = None, legs: str | None = None,
               width: int = 2):
        from shunkan.data.chains import get_chain
        from shunkan.derivatives import analyze_payoff, build_strategy, parse_custom_legs

        try:
            c = get_chain(symbol)
            if legs:
                a = analyze_payoff(c, parse_custom_legs(c, legs.split(",")), name="custom")
            else:
                a = build_strategy(c, strategy or "iron_condor", width=width)
        except (DataError, ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        step = max(len(a.grid) // 240, 1)  # ~240 points is plenty for the chart
        return _clean({
            "symbol": a.symbol, "name": a.name, "spot": a.spot,
            # lot_size is null when no source could name the contract lot —
            # then every rupee figure below is per unit, and per_lot says so.
            "lot_size": a.lot_size, "per_lot": a.lot_size is not None,
            "expiry": str(c.expiry),
            "source": c.source,
            "legs": [leg.describe() for leg in a.legs],
            "curve": [
                {"x": float(a.grid[i]),
                 "y": float(a.payoff_per_unit[i] * (a.lot_size or 1))}
                for i in range(0, len(a.grid), step)
            ],
            "breakevens": a.breakevens,
            "max_profit": None if a.max_profit == float("inf") else a.max_profit,
            "max_loss": None if a.max_loss == float("-inf") else a.max_loss,
            "unlimited_profit": a.max_profit == float("inf"),
            "unlimited_loss": a.max_loss == float("-inf"),
            "net_premium_lot": a.net_premium * (a.lot_size or 1),
            "pop": a.pop, "greeks": a.greeks,
            "prov": {
                "pop": prov(
                    "POP = P(payoff > 0) under lognormal terminal price",
                    {"spot": (a.spot, c.source),
                     "σ (ATM IV)": f"{_atm_iv_for_prov(c):.4f}",
                     "T": f"{c.t_years * 365:.1f} days",
                     "grid": f"{len(a.grid)} price points"},
                    c.source,
                    method="bin probabilities from the lognormal CDF summed over profitable price regions",
                    caveat="MODEL probability, not market-implied; ignores smile/skew and early management",
                ),
                "greeks": prov(
                    "Black-Scholes greeks per leg, summed (side-weighted), "
                    + ("× lot size" if a.lot_size else "per unit (lot unknown)"),
                    {"r": "6.5% (10y G-sec ballpark)",
                     "IV per leg": "solved from that leg's market premium",
                     "lot": c.lot_size or "unknown — figures are per unit"},
                    c.source,
                    caveat="European-exercise model; theta in ₹/calendar-day, vega per 1 vol point",
                ),
            },
        })

    @app.get("/api/iv/{symbol}")
    def iv(symbol: str):
        from shunkan.data.chains import get_chain
        from shunkan.derivatives import analyze_vol
        from shunkan.store import iv_rank_local
        from shunkan.store.store import atm_iv_intraday

        try:
            c = get_chain(symbol)
            hist = provider.history(symbol, period="1y", interval="1d")
            r = analyze_vol(c, hist)
        except (DataError, ValueError) as exc:
            raise HTTPException(502, str(exc)) from exc

        rank = iv_rank_local(c.symbol, r.atm_iv) if not math.isnan(r.atm_iv) else {
            "available": False, "days_captured": 0,
            "days_required": 20, "note": "no ATM IV available",
        }
        hist_src = "Yahoo daily closes" if not is_offline() else "synthetic (offline)"
        return _clean({
            "symbol": r.symbol, "spot": r.spot, "expiry": str(c.expiry),
            # The chain's own exchange timestamp — the age the panel shows is
            # the age of the QUOTES, not of the render.
            "as_of": c.as_of.isoformat() if c.as_of else None,
            "atm_iv": r.atm_iv, "rv_cc_21": r.rv_cc_21, "rv_park_21": r.rv_park_21,
            "iv_premium": r.iv_premium, "rv_percentile": r.rv_percentile,
            "iv_rank_local": rank,
            # Today's captured ATM-IV path (60s cadence while the session is
            # open and a token is live). Empty list = nothing captured yet
            # today, which the panel says instead of drawing a flat line.
            "intraday": atm_iv_intraday(c.symbol),
            "cone": {str(d): list(v) for d, v in r.cone.items()},
            "smile": [
                {"strike": float(r.smile_strikes[i]),
                 "call_iv": _clean(float(r.smile_call_iv[i])),
                 "put_iv": _clean(float(r.smile_put_iv[i]))}
                for i in range(len(r.smile_strikes))
            ],
            "notes": r.notes,
            "prov": {
                "rv_cc_21": prov(
                    "σ_realized = std(ln(Pt/Pt−1), 21 bars) × √252",
                    {"window": "21 trading days", "bars in history": len(hist)},
                    hist_src,
                    method="close-to-close estimator, sample std (ddof=1)",
                ),
                "rv_park_21": prov(
                    "σ_park = √( mean(ln(H/L)², 21 bars) / (4·ln2) × 252 )",
                    {"window": "21 trading days"},
                    hist_src,
                    method="Parkinson range estimator — uses intrabar high/low, ~5x more sample-efficient",
                ),
                "iv_premium": prov(
                    "IV premium = ATM IV − realized σ (close-close, 21d)",
                    {"ATM IV": f"{r.atm_iv:.4f}" if not math.isnan(r.atm_iv) else "—",
                     "realized 21d": f"{r.rv_cc_21:.4f}" if not math.isnan(r.rv_cc_21) else "—"},
                    f"{c.source} + {hist_src}",
                    caveat="positive = options rich vs recent movement; says nothing about future realized",
                ),
                "rv_percentile": prov(
                    "percentile of today's 21d realized vol within its own 1y distribution",
                    {"observations": int(len(hist)) },
                    hist_src,
                    caveat="REALIZED-vol percentile — a stand-in until local IV history accumulates (see IV rank)",
                ),
                "iv_rank_local": prov(
                    "rank = share of locally captured daily ATM-IV observations ≤ today's",
                    {"days captured": rank.get("days_captured", 0),
                     "days required": rank.get("days_required", 20),
                     **({"window": f"{rank.get('first_day')} → {rank.get('last_day')}"}
                        if rank.get("available") else {})},
                    "local chain store (real captured snapshots only)",
                    caveat="refuses to report below minimum history rather than fabricate a rank",
                ),
                "cone": prov(
                    "band(d) = spot × exp(±n·σ·√(d/252)), n ∈ {1,2}",
                    {"σ (ATM IV)": f"{r.atm_iv:.4f}" if not math.isnan(r.atm_iv) else "—",
                     "spot": (r.spot, c.source)},
                    c.source,
                    caveat="lognormal model at a flat ATM vol — ignores smile/skew by construction",
                ),
            },
        })

    @app.get("/api/volume/{symbol}")
    def volume(symbol: str, period: str = "6mo"):
        from shunkan.analytics.volume import analyze_volume

        try:
            hist = provider.history(symbol, period=period, interval="1d")
            r = analyze_volume(hist)
        except (DataError, ValueError) as exc:
            raise HTTPException(502, str(exc)) from exc
        prof = r.profile
        mids = 0.5 * (prof.bin_edges[:-1] + prof.bin_edges[1:])
        # A 0.00x surge ratio is absence dressed as data: index tapes print
        # no intraday volume (today's bar reads zero until EOD, if ever), so
        # the ratio of today-to-average divides nothing by something. Refuse
        # the surge metrics with the reason; the price structure stays.
        no_volume = float(hist["volume"].fillna(0).iloc[-1]) <= 0
        last_bar = hist.index[-1]
        return _clean({
            "symbol": symbol.upper(),
            # Daily bars: the analysis is true as of the last bar's session
            # close, which on a live day is today and after hours is
            # yesterday — the stamp shows which, instead of implying "now".
            "as_of": (last_bar.isoformat() if hasattr(last_bar, "isoformat")
                      else str(last_bar)),
            "last_close": float(hist["close"].iloc[-1]),
            "day_type": (r.day_type if not no_volume
                         else "no volume printed (index tape)"),
            "surge_z": None if no_volume else r.surge_z,
            "surge_ratio": None if no_volume else r.surge_ratio,
            "volume_note": ("the latest bar prints no volume (index tape, or "
                            "the session's total lands at EOD); surge and OBV "
                            "need a real tape" if no_volume else None),
            "obv_divergence": "none" if no_volume else r.obv_divergence,
            "poc": prof.poc, "value_area": [prof.value_area_low, prof.value_area_high],
            "profile": [
                {"price": float(mids[i]), "volume": float(prof.volume_at_price[i])}
                for i in range(len(mids))
            ],
            "notes": r.notes,
            "prov": {
                "surge_z": prov(
                    "z = (V_today − mean(V, prior 20 bars)) / std(V, prior 20 bars)",
                    {"today volume": float(hist["volume"].iloc[-1]),
                     "window": "20 bars (excl. today)"},
                    "Yahoo daily bars" if not is_offline() else "synthetic (offline)",
                    caveat="zero when trailing variance is degenerate",
                ),
                "poc": prov(
                    "price bin with max volume; each bar's volume spread across the bins its H–L range covers",
                    {"bins": len(mids), "lookback": "120 bars",
                     "value area": "smallest POC-centered region holding 70% of volume"},
                    "Yahoo daily bars" if not is_offline() else "synthetic (offline)",
                ),
            },
        })

    # -- news -----------------------------------------------------------------

    @app.get("/api/news")
    def news(symbol: str | None = None, limit: int = 20):
        from shunkan.intel import aggregate_bias, assess_impact, summarize
        from shunkan.intel.feeds import fetch_news, symbol_news
        from shunkan.intel.sentiment import score_sentiment_detailed, sentiment_label

        try:
            items = symbol_news(symbol, limit) if symbol else fetch_news(limit=limit)
        except Exception as exc:
            raise HTTPException(502, f"News fetch failed: {exc}") from exc
        # Newest first, always. Google returns RELEVANCE order, and rendering
        # it raw made the ages jump 28m -> 6h -> 2h down the page.
        items = sorted(items, key=lambda i: (i.published is None,
                                             -(i.published.timestamp() if i.published else 0)))
        # Sector tags from NSE's own Industry column; a failed fetch degrades
        # to untagged headlines, never to a broken news screen.
        try:
            from shunkan.data.constituents import (alias_table, industry_map,
                                                   map_title, universe)

            _uni = universe()
            _aliases = alias_table(_uni)
            _industry = industry_map(_uni)
        except Exception:
            _aliases, _industry = [], {}
        out = []
        for item in items:
            call = assess_impact(item)
            summary = summarize(item.description or "", max_sentences=1)
            detail = score_sentiment_detailed(f"{item.title}. {item.description}")
            comp = call.components
            syms = map_title(item.title, _aliases) if _aliases else []
            sectors = sorted({_industry[s] for s in syms if s in _industry})
            out.append(_clean({
                "title": item.title, "source": item.source, "link": item.link,
                "published": item.published.isoformat() if item.published else None,
                "age_minutes": round(item.age_hours * 60.0, 1),
                "symbols": syms,
                # one sector per row: the first matched company's industry;
                # cross-sector stories land under the first and name the rest
                "sector": sectors[0] if sectors else "",
                "sectors_all": sectors,
                "sentiment": item.sentiment,
                "sentiment_label": sentiment_label(item.sentiment),
                "summary": summary,
                "impact": {
                    "category": call.category, "direction": call.direction,
                    "confidence": call.confidence, "horizon": call.horizon,
                    "segment": call.segment, "magnitude": call.magnitude,
                },
                "prov": {
                    "sentiment": prov(
                        "lexicon hits with negation (×−0.8) and intensifiers (×1.5); score = Σw / hits^0.7 × 0.6, clamped [−1,1]",
                        {"bullish terms": ", ".join(detail["pos_terms"][:6]) or "none",
                         "bearish terms": ", ".join(detail["neg_terms"][:6]) or "none",
                         "scored terms": detail["hits"]},
                        f"{item.source} via Google News RSS",
                        method="finance lexicon (Loughran-McDonald-style + India vocab); 'cut' is bullish only in rate context",
                    ),
                    "confidence": prov(
                        comp.get("formula", ""),
                        {"sentiment": comp.get("sentiment"),
                         "category weight": (comp.get("category_weight"), f"taxonomy: {call.category}"),
                         "equity-direction flip": comp.get("sentiment_flip"),
                         "timing multiplier": (comp.get("timing_multiplier"), f"landed {call.phase} IST"),
                         "strength": comp.get("strength")},
                        "rule-based impact model",
                        caveat="heuristic decision support, capped at 85% — markets routinely defy the obvious read",
                    ),
                },
            }))
        bias = aggregate_bias(items)
        return {
            "items": out,
            "bias": _clean({
                "score": bias.score, "label": bias.label,
                "gap_call": bias.gap_call, "n_items": bias.n_items,
                "prov": prov(
                    "bias = Σ(direction·confidence·0.5^(age_h/6)) / Σ(confidence·0.5^(age_h/6))",
                    {"headlines scored": bias.n_items,
                     "recency decay": "6h half-life",
                     "top drivers": "; ".join(bias.drivers[:2]) or "none"},
                    "per-headline impact calls (each has its own ⓘ)",
                    caveat="aggregate of heuristics; calibration tracking not yet implemented",
                ),
            }),
            "feed_note": (
                "Headlines via Google News RSS (India). Aggregator latency is "
                "typically 5–15 min behind the original wire; ages shown use "
                "publish time. Treat very fresh items as possibly already priced in."
            ),
        }

    # -- backtesting ------------------------------------------------------------

    @app.get("/api/strategies")
    def strategies():
        from shunkan.backtest import STRATEGIES

        return {
            name: {"description": s.description, "defaults": s.defaults,
                   "param_grid": s.param_grid}
            for name, s in sorted(STRATEGIES.items())
        }

    @app.post("/api/backtest")
    def backtest(req: BacktestRequest):
        from shunkan.backtest import (
            BacktestConfig, get_strategy, monte_carlo, run_backtest, walk_forward,
        )

        try:
            strat = get_strategy(req.strategy)
            hist = provider.history(req.symbol, period=req.period, interval="1d")
        except (DataError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        params = {k: (int(v) if float(v) == int(v) else float(v)) for k, v in req.params.items()}

        if req.mode == "walkforward":
            wf = walk_forward(hist, strat, symbol=req.symbol)
            return _clean({
                "mode": "walkforward",
                "windows": [
                    {"test_start": str(w.test_start)[:10], "is_sharpe": w.is_sharpe,
                     "oos_sharpe": w.oos_sharpe, "oos_return": w.oos_return,
                     "params": w.best_params}
                    for w in wf.windows
                ],
                "oos_return": wf.oos_return, "oos_sharpe": wf.oos_sharpe,
                "oos_max_dd": wf.oos_max_dd, "efficiency": wf.efficiency,
                "param_stability": wf.param_stability, "verdict": wf.verdict,
                "equity": _series(wf.oos_equity / wf.oos_equity.iloc[0])
                if wf.oos_equity is not None else [],
            })

        bt = run_backtest(hist, strat.signal(hist, **params), BacktestConfig(),
                          symbol=req.symbol, strategy_name=strat.name,
                          params={**strat.defaults, **params})
        if req.mode == "validate":
            # The gate. `search` is None here because this endpoint runs one
            # named strategy with given parameters and did not search: that is
            # the honest reading, and it is also the most generous one, so a
            # rejection here is not an artefact of an inflated trial count.
            from shunkan.backtest.validate import validate

            v = validate(bt, search=None)
            return _clean({
                "mode": "validate",
                "passes": v.passes,
                "verdict": v.verdict(),
                "trials": {"n": v.trials.n_trials, "source": v.trials.source},
                "permutation": {
                    "p_value": v.permutation.p_value,
                    "observed_sharpe": v.permutation.observed_sharpe,
                    "percentile": v.permutation.percentile,
                    "significant": v.permutation.significant,
                    "verdict": v.permutation.verdict(),
                },
                "deflated_sharpe": {
                    "observed": v.deflation.observed_sharpe,
                    "expected_max_from_trials": v.deflation.expected_max_sharpe,
                    "dsr": v.deflation.deflated,
                    "survives": v.deflation.survives,
                    "verdict": v.deflation.verdict(),
                },
                "prov": prov(
                    "strategy validation",
                    {"trials": v.trials.n_trials,
                     "permutations": v.permutation.n_permutations},
                    "in-sample only",
                    method="block permutation of the position series against the "
                           "real bar returns, plus Bailey & Lopez de Prado "
                           "deflated Sharpe; both must pass",
                    caveat="in-sample. Neither test substitutes for out-of-sample "
                           "data. The permutation test cannot see selection bias "
                           "and the deflated Sharpe cannot see whether the timing "
                           "does anything, which is why the gate requires both.",
                ),
            })

        if req.mode == "montecarlo":
            mc = monte_carlo(bt.returns)
            n = mc.n_bars
            idx = bt.equity.index
            return _clean({
                "mode": "montecarlo", "n_paths": mc.n_paths,
                "terminal": {"p5": mc.terminal_p5, "p50": mc.terminal_p50,
                             "p95": mc.terminal_p95},
                "prob_loss": mc.prob_loss, "max_dd_median": mc.max_dd_median,
                "max_dd_p95": mc.max_dd_p95, "verdict": mc.verdict(),
                "bands": {
                    "p5": _band(idx, mc.envelope_p5),
                    "p50": _band(idx, mc.envelope_p50),
                    "p95": _band(idx, mc.envelope_p95),
                    "actual": _series(bt.equity / bt.initial_cash),
                },
            })

        bench = run_backtest(hist, get_strategy("buy_hold").signal(hist),
                             BacktestConfig(), symbol=req.symbol)
        return _clean({
            "mode": "backtest",
            "metrics": bt.metrics(),
            "summary": bt.summary_rows(),
            "bench_return": bench.total_return,
            "equity": _series(bt.equity / bt.initial_cash),
            "bench_equity": _series(bench.equity / bench.initial_cash),
        })

    @app.get("/api/builder/indicators")
    def builder_indicators():
        """Catalog powering the visual builder UI — indicators, operators, and
        the SL/TP and timeframe choices. Shared source of truth with the engine."""
        from shunkan.backtest import INDICATORS, OPERATORS
        from shunkan.data.provider import VALID_INTERVALS

        return {
            "indicators": INDICATORS,
            "operators": OPERATORS,
            "sl_tp_modes": ["none", "percent", "pips", "atr"],
            "intervals": VALID_INTERVALS,
        }

    @app.post("/api/backtest/build")
    def backtest_build(req: BuilderRequest):
        from shunkan.backtest import ExecConfig, RuleSpec, compile_spec, simulate
        from shunkan.data.provider import VALID_INTERVALS

        if req.interval not in VALID_INTERVALS:
            raise HTTPException(400, f"interval must be one of {VALID_INTERVALS}")
        try:
            spec = RuleSpec.from_dict(req.spec)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, f"invalid strategy: {exc}") from exc
        try:
            hist = provider.history(req.symbol, period=req.period, interval=req.interval)
        except DataError as exc:
            # No-fake-numbers: refuse rather than synthesize unavailable intraday data.
            raise HTTPException(502, str(exc)) from exc
        if len(hist) < 30:
            raise HTTPException(
                400,
                f"only {len(hist)} bars for {req.symbol} at {req.interval} — too few to "
                "backtest; widen the period or use a coarser timeframe",
            )

        cfg = ExecConfig(
            initial_cash=req.initial_cash, commission=req.commission, slippage=req.slippage,
            sl_mode=req.sl_mode, sl_value=req.sl_value, tp_mode=req.tp_mode,
            tp_value=req.tp_value, trailing=req.trailing, atr_period=req.atr_period,
            pip_size=_pip_size(req.symbol), session_start=req.session_start,
            session_end=req.session_end, cooldown_bars=req.cooldown_bars,
            atr_min=req.atr_min, atr_max=req.atr_max, allow_short=req.allow_short,
            params={"interval": req.interval, "rule": spec.describe()},
        )
        sig = compile_spec(hist, spec)
        bt = simulate(hist, sig, cfg, symbol=req.symbol.upper())

        close = hist[[c for c in hist.columns if c.lower() == "close"][0]].astype(float)
        bench_curve = close / float(close.iloc[0])
        risk = _exec_summary(cfg)
        return _clean({
            "mode": "builder",
            "symbol": req.symbol.upper(),
            "interval": req.interval,
            "bars": len(hist),
            "offline": is_offline(),
            "rule": spec.describe(),
            "metrics": bt.metrics(),
            "summary": bt.summary_rows(),
            "bench_return": float(bench_curve.iloc[-1] - 1.0),
            "equity": _series(bt.equity / bt.initial_cash),
            "bench_equity": _series(bench_curve),
            "trades": [
                {"entry_time": t.entry_time.isoformat(), "exit_time": t.exit_time.isoformat(),
                 "direction": t.direction, "entry_price": t.entry_price,
                 "exit_price": t.exit_price, "return_pct": t.return_pct,
                 "bars_held": t.bars_held, "exit_reason": t.exit_reason}
                for t in bt.trades
            ],
            "exit_breakdown": _exit_breakdown(bt.trades),
            "data_note": (
                "Synthetic offline data — set SHUNKAN_OFFLINE=0 for real history."
                if is_offline() else
                f"{len(hist)} {req.interval} bars of real history."
            ),
            "prov": {
                "execution": prov(
                    "next-bar-open fills; intrabar stop/target from bar high/low; "
                    "stop assumed before target when a bar spans both",
                    {"stop": risk["stop"], "target": risk["target"],
                     "trailing": cfg.trailing, "cost/side": f"{(cfg.commission + cfg.slippage):.4%}",
                     "filters": risk["filters"]},
                    "Shunkan event-driven simulator",
                    method="one-bar signal delay avoids look-ahead; equity marks to close each bar",
                    caveat="fully-invested per position (no leverage/lot sizing); fills assume "
                    "your size doesn't move the market",
                ),
                "metrics": prov(
                    "Sharpe/Sortino annualised from per-bar returns; profit factor & win rate "
                    "from realised round trips",
                    {"trades": len(bt.trades), "bars": len(hist),
                     "periods/yr": "252 (bar-count based)"},
                    "Shunkan stats on the simulated equity curve",
                    caveat="intraday Sharpe annualised on a 252 bar-per-year basis is approximate",
                ),
            },
        })

    # -- quant lab 3d ------------------------------------------------------------

    @app.get("/api/viz/surface/{symbol}")
    def viz_surface(symbol: str):
        from shunkan.analytics.viz import iv_surface
        from shunkan.data.chains import get_chain

        try:
            c = get_chain(symbol)
            s = iv_surface(c)
        except (DataError, ValueError) as exc:
            raise HTTPException(502, str(exc)) from exc
        return _clean({
            "symbol": s.symbol, "spot": s.spot, "source": s.source,
            "strikes": s.strikes.tolist(), "days": s.days.tolist(),
            "iv": s.iv.tolist(), "atm_iv": s.atm_iv,
            "chain_days": s.chain_days, "market_row": s.market_row,
            "elapsed_ms": s.elapsed_ms,
            "prov": {
                "surface": prov(
                    "IV(K,T) = ATM_IV + (smile(K) − ATM_IV) · √(T_chain / T)",
                    {"ATM IV": (f"{s.atm_iv:.1%}", s.source),
                     "smile strikes": len(s.strikes),
                     "market expiry (days)": round(s.chain_days, 1)},
                    s.source,
                    method="the smile at the chain's own expiry is market data (IVs "
                    "solved from traded premiums); other maturities damp the smile's "
                    "deviation from ATM by √(T_chain/T) — sticky-moneyness flattening",
                    caveat="only the highlighted T_chain slice is market data; other "
                    "rows are a documented model extension, not quotes",
                ),
            },
        })

    @app.get("/api/viz/greeks/{symbol}")
    def viz_greeks(symbol: str, greek: str = "gamma", side: str = "call"):
        from shunkan.analytics.viz import greeks_surface
        from shunkan.data.chains import get_chain
        from shunkan.derivatives.chain import analyze_chain

        try:
            c = get_chain(symbol)
            a = analyze_chain(c)
            g = greeks_surface(c.spot, a.atm_iv, greek=greek,
                               is_call=(side != "put"))
        except (DataError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return _clean({
            "symbol": c.symbol, "greek": g.greek, "side": side,
            "spot": g.spot, "sigma": g.sigma, "source": c.source,
            "strikes": g.strikes.tolist(), "days": g.days.tolist(),
            "values": g.values.tolist(), "elapsed_ms": g.elapsed_ms,
            "prov": {
                "surface": prov(
                    f"Black-Scholes {g.greek}(K, T) at live spot and ATM IV",
                    {"spot": (g.spot, c.source),
                     "ATM IV": (f"{g.sigma:.1%}", c.source),
                     "r": "6.5% (Indian risk-free ballpark)"},
                    c.source,
                    method="one broadcast bs_greeks call over the strike × maturity "
                    "meshgrid; theta per calendar day, vega per vol point",
                    caveat="model surface at a single flat IV — real gamma/vega "
                    "concentrations shift with the smile",
                ),
            },
        })

    @app.get("/api/viz/montecarlo/{symbol}")
    def viz_montecarlo(symbol: str, horizon: int = 60, paths: int = 2000):
        from shunkan.analytics.viz import price_fan

        horizon = max(10, min(horizon, 250))
        paths = max(200, min(paths, 5000))
        try:
            hist = provider.history(symbol, period="2y", interval="1d")
            spot = float(hist["Close"].iloc[-1]) if "Close" in hist.columns \
                else float(hist["close"].iloc[-1])
            f = price_fan(hist, spot, symbol=symbol,
                          horizon_days=horizon, n_paths=paths)
        except (DataError, ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return _clean({
            "symbol": f.symbol, "spot": f.spot, "horizon_days": f.horizon_days,
            "n_paths": f.n_paths, "hist_bars": f.hist_bars,
            "days": f.days.tolist(),
            "paths": f.display_paths.tolist(),
            "envelope": {k: v.tolist() for k, v in f.envelope.items()},
            "terminal_bins": f.terminal_bins.tolist(),
            "terminal_freq": f.terminal_freq.tolist(),
            "prob_up": f.prob_up, "elapsed_ms": f.elapsed_ms,
            "prov": {
                "fan": prov(
                    "block bootstrap of the instrument's own daily returns",
                    {"paths": f.n_paths, "block size": f.block_size,
                     "history bars": (f.hist_bars, "2y daily closes"),
                     "anchor spot": f.spot},
                    "resampled real return history",
                    method="contiguous return blocks preserve fat tails and "
                    "short-range autocorrelation; no normality assumed",
                    caveat="resampling the past is not a forecast — regime "
                    "changes are exactly what it cannot see",
                ),
            },
        })

    @app.get("/api/sessions")
    def sessions():
        from shunkan.markets import world_sessions

        return {"exchanges": world_sessions(),
                "caveat": "regular cash hours with lunch breaks; exchange "
                          "holidays are not modeled"}

    def _hist(symbol: str, period: str = "1y", interval: str = "1d",
              start: str | None = None, end: str | None = None):
        """History with optional exact date window — the researcher's knob.
        start/end slice a period=max fetch, so any provider supports it."""
        if not (start or end):
            return provider.history(symbol, period=period, interval=interval)
        h = provider.history(symbol, period="max", interval=interval)
        idx = h.index.tz_localize(None) if getattr(h.index, "tz", None) is not None \
            else h.index
        mask = np.ones(len(h), dtype=bool)
        if start:
            mask &= np.asarray(idx >= pd.Timestamp(start))
        if end:
            mask &= np.asarray(idx <= pd.Timestamp(end))
        h = h.loc[mask]
        if len(h) < 30:
            raise DataError(f"Only {len(h)} bars for {symbol} in "
                            f"[{start or 'begin'} … {end or 'now'}] — widen the window")
        return h

    def _universe_closes(universe: str, period: str, cap: int = 12,
                         start: str | None = None, end: str | None = None):
        from shunkan.screener import UNIVERSES

        base = {
            "indices": ["NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX", "USDINR",
                        "^GSPC", "^IXIC", "^N225", "^HSI", "GC=F", "BZ=F"],
        }
        symbols = base.get(universe) or UNIVERSES.get(universe)
        if not symbols:
            raise HTTPException(400, f"Unknown universe '{universe}'. "
                                f"Available: indices, {', '.join(UNIVERSES)}")
        closes, failed = {}, []
        for s in symbols[:cap]:  # keep the fetch fan-out bounded
            try:
                h = _hist(s, period=period, start=start, end=end)
                col = "Close" if "Close" in h.columns else "close"
                closes[s] = h[col].astype(float)
            except (DataError, KeyError, ValueError):
                failed.append(s)
        return closes, failed

    @app.get("/api/viz/correlation")
    def viz_correlation(universe: str = "indices", period: str = "6mo",
                        start: str | None = None, end: str | None = None):
        from shunkan.analytics.viz import correlation_matrix

        closes, failed = _universe_closes(universe, period, start=start, end=end)
        try:
            r = correlation_matrix(closes)
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc
        return _clean({
            "universe": universe, "period": period,
            "symbols": r.symbols, "matrix": r.matrix.tolist(),
            "n_obs": r.n_obs, "avg_corr": r.avg_corr,
            "top_pairs": [{"a": a, "b": b, "corr": c} for a, b, c in r.top_pairs],
            "hedge_pairs": [{"a": a, "b": b, "corr": c} for a, b, c in r.hedge_pairs],
            "dropped": r.dropped + failed, "elapsed_ms": r.elapsed_ms,
            "prov": {
                "matrix": prov(
                    "Pearson correlation of overlapping daily close-to-close returns",
                    {"symbols": len(r.symbols), "overlapping days": r.n_obs,
                     "period": period,
                     "dropped (thin data)": ", ".join(r.dropped + failed) or "none"},
                    "daily closes via the active data provider",
                    method="rows with any missing return are excluded pairwise-"
                    "consistently (common date range)",
                    caveat="correlation is regime-dependent — a 6-month number "
                    "says nothing about the next crisis day",
                ),
            },
        })

    @app.get("/api/viz/var")
    def viz_var(universe: str = "indices", period: str = "1y",
                start: str | None = None, end: str | None = None):
        from shunkan.analytics.viz import var_analysis

        closes, failed = _universe_closes(universe, period, start=start, end=end)
        try:
            r = var_analysis(closes)
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc
        return _clean({
            "universe": universe, "period": period, "symbols": r.symbols,
            "horizons": r.horizons.tolist(),
            "var_curve": r.var_curve.tolist(), "es_curve": r.es_curve.tolist(),
            "p95_curve": r.p95_curve.tolist(),
            "surface_bins": r.surface_bins.tolist(),
            "surface": r.surface.tolist(),
            "alpha": r.alpha, "n_obs": r.n_obs, "n_paths": r.n_paths,
            "failed": failed, "elapsed_ms": r.elapsed_ms,
            "prov": {
                "var": prov(
                    f"VaR({r.alpha:.0%}) = −P{r.alpha * 100:.0f} of bootstrapped "
                    "basket P&L; ES = mean loss beyond VaR",
                    {"basket": f"equal-weight {len(r.symbols)} symbols",
                     "paths": r.n_paths, "block size": r.block_size,
                     "overlapping days": r.n_obs},
                    "block bootstrap of the basket's own joint return history",
                    method="the basket series is formed before resampling, so "
                    "cross-correlation between names is embedded in every draw",
                    caveat="history-bounded: a loss larger than anything in the "
                    "sample window cannot appear in the distribution",
                ),
            },
        })

    @app.get("/api/viz/frontier")
    def viz_frontier(universe: str = "nifty50", period: str = "1y",
                     start: str | None = None, end: str | None = None):
        from shunkan.analytics.viz import efficient_frontier

        closes, failed = _universe_closes(universe, period, start=start, end=end)
        try:
            r = efficient_frontier(closes)
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc
        return _clean({
            "universe": universe, "period": period, "symbols": r.symbols,
            "points": r.points.tolist(),
            "max_sharpe": r.max_sharpe, "min_vol": r.min_vol,
            "rf": r.rf, "n_portfolios": r.n_portfolios, "n_obs": r.n_obs,
            "failed": failed, "elapsed_ms": r.elapsed_ms,
            "prov": {
                "frontier": prov(
                    "Sharpe = (w·μ − rf) / √(wᵀΣw), μ and Σ annualized ×252",
                    {"portfolios": r.n_portfolios,
                     "symbols": len(r.symbols), "rf": f"{r.rf:.1%}",
                     "overlapping days": r.n_obs},
                    "random long-only Dirichlet portfolios on shared-calendar returns",
                    method="one matrix pass over all portfolios; no optimizer, "
                    "the hull of the cloud IS the attainable frontier",
                    caveat="μ estimated from one year of history is the noisiest "
                    "input in finance — weights are illustrative, not advice",
                ),
            },
        })

    def _compose_daily(sym: str, on_date=None) -> dict:
        """The daily analysis composition, shared by the live route, the
        ?on= replay, and the close-of-day journal writer.

        Live (on_date None): archive + live chain + live news. Replay: every
        section rebuilt from what the stores held ON that day - positioning
        from the day's last captured snapshot, never a re-fetch wearing an
        old date. Facts and base rates only, no verdict by design.
        """
        import numpy as np

        from shunkan.analytics.daily import (
            participants_asof,
            positioning_from_snapshot,
            vwap_today,
        )
        from shunkan.analytics.events import classify_today, event_study
        from shunkan.data.participant import latest_with_change
        from shunkan.markets import INDEX_ALIASES
        from shunkan.store.store import ChainStore, STORE_DIR

        out: dict = {"symbol": sym,
                     "served_from": "live" if on_date is None
                     else "reconstructed from archives"}

        # ---- root: the underlying, from the local archive -------------------
        def archive_frame():
            cands = [sym]
            alias = INDEX_ALIASES.get(sym)
            if alias:
                cands.append(alias.replace("^", "_").replace("=", "_").replace("/", "_"))
            for c in cands:
                f = STORE_DIR / "history" / f"{c}.parquet"
                if f.exists():
                    df = pd.read_parquet(f).sort_values("date")
                    df["date"] = pd.to_datetime(df["date"])
                    return df
            raise DataError(f"no archived history for {sym} (tried {cands})")

        try:
            hist = archive_frame()
            if on_date is not None:
                hist = hist[hist["date"] <= pd.Timestamp(on_date)]
                if hist.empty or hist.iloc[-1]["date"].date() != on_date:
                    raise DataError(f"no {sym} session on {on_date} in the archive "
                                    "(weekend, holiday, or before coverage)")
            row, prev = hist.iloc[-1], hist.iloc[-2]
            close = hist.set_index("date")["close"]
            r = np.log(close).diff()
            week = close.resample("W-FRI").agg(["first", "last"]).dropna()
            wk, wk_prev = week.iloc[-1], week.iloc[-2]
            # candle facts, defined patterns only, no chart-reading mysticism
            bearish_engulf = (row.close < row.open and prev.close > prev.open
                              and row.open >= prev.close and row.close <= prev.open)
            bullish_engulf = (row.close > row.open and prev.close < prev.open
                              and row.open <= prev.close and row.close >= prev.open)
            rng_lo, rng_hi = float(row.low), float(row.high)
            out["as_of"] = row.date.date().isoformat()
            out["chart"] = {
                "close": float(row.close),
                "chg_pct": float(row.close / prev.close - 1) * 100,
                "gap_pct": float(row.open / prev.close - 1) * 100,
                "intraday_pct": float(row.close / row.open - 1) * 100,
                "range_pos_pct": (float((row.close - rng_lo) / (rng_hi - rng_lo)) * 100
                                  if rng_hi > rng_lo else None),
                "week_chg_pct": float(wk["last"] / wk["first"] - 1) * 100,
                "prev_week_chg_pct": float(wk_prev["last"] / wk_prev["first"] - 1) * 100,
                "daily_candle": ("bearish engulfing" if bearish_engulf
                                 else "bullish engulfing" if bullish_engulf
                                 else "red" if row.close < row.open else "green"),
                "rv5": float(r.tail(5).std() * np.sqrt(252) * 100),
                "rv21": float(r.tail(21).std() * np.sqrt(252) * 100),
            }
            if on_date is None:
                # day-structure read from the locally captured tape
                vwap, vwap_note = vwap_today(sym)
                out["chart"]["vwap"] = vwap
                out["chart"]["vwap_note"] = vwap_note
            # base rates for a day like today, and the standard studies either way
            out["events"] = {
                "today": classify_today(close),
                "down_2s": event_study(close, sym, sigma=2.0, direction="down").to_dict(),
                "up_2s": event_study(close, sym, sigma=2.0, direction="up").to_dict(),
            }
        except (DataError, IndexError, KeyError) as exc:
            out["chart"] = {"error": str(exc)[:160]}
            out["events"] = {"error": str(exc)[:160]}

        # ---- vol context: VIX percentile from our own 2008+ series ----------
        try:
            vix = pd.read_parquet(STORE_DIR / "history" / "_INDIAVIX.parquet")
            vix = vix.sort_values("date")
            vix["date"] = pd.to_datetime(vix["date"])
            if on_date is not None:
                vix = vix[vix["date"] <= pd.Timestamp(on_date)]
            if vix.empty:
                raise DataError("VIX series empty for that date")
            v = float(vix["close"].iloc[-1])
            out["vol"] = {
                "vix": v,
                "vix_date": pd.Timestamp(vix["date"].iloc[-1]).date().isoformat(),
                "vix_pctile": float((vix["close"] < v).mean() * 100),
            }
        except Exception as exc:
            out["vol"] = {"error": f"VIX series unavailable: {str(exc)[:120]}"}

        # ---- derivatives positioning ---------------------------------------
        if on_date is None:
            try:
                from shunkan.data.chains import get_chain
                from shunkan.derivatives.chain import analyze_chain
                from shunkan.markets import today_ist

                c = get_chain(sym)
                a = analyze_chain(c)
                out["positioning"] = {
                    "expiry": str(c.expiry), "source": c.source, "is_model": c.is_model,
                    "spot": c.spot, "pcr_oi": a.pcr_oi, "max_pain": a.max_pain,
                    "dist_to_max_pain_pct": (a.max_pain / c.spot - 1) * 100 if c.spot else None,
                    "support": a.support, "resistance": a.resistance,
                    "atm_iv_pct": a.atm_iv * 100 if a.atm_iv else None,
                    "straddle": a.straddle_price,
                    "implied_move_pct": a.expected_move_pct * 100,
                }
                if str(c.expiry) == today_ist().isoformat():
                    # Measured 2026-08-18: Monday's max pain 24,350 re-anchored
                    # to 24,200 within the first hours and price never visited
                    # the old level. Say it where the numbers are shown.
                    out["positioning"]["expiry_today"] = True
                    out["positioning"]["staleness_warning"] = (
                        "expiry day: overnight OI maps expire at the open - "
                        "walls re-form intraday; read the migration, not "
                        "yesterday's levels")
            except (DataError, ValueError) as exc:
                out["positioning"] = {"error": str(exc)[:200],
                                      "source_trail": list(getattr(exc, "source_trail", []))}
        else:
            try:
                snap = ChainStore().last_snapshot_of_day(sym, on_date)
                if snap is None or snap.empty:
                    raise DataError(f"no chain snapshot captured on {on_date}")
                front = snap[snap["expiry"] == snap["expiry"].min()]
                out["positioning"] = positioning_from_snapshot(front)
            except (DataError, ValueError) as exc:
                out["positioning"] = {"error": str(exc)[:200]}

        # ---- participants: NSE's own who-did-what table ---------------------
        try:
            part = (latest_with_change() if on_date is None
                    else participants_asof(on_date))
            out["participants"] = part if part else {
                "error": "fewer than two participant days on file for that date"}
        except Exception as exc:
            out["participants"] = {"error": str(exc)[:160]}

        # ---- news -----------------------------------------------------------
        if on_date is None:
            try:
                from shunkan.intel import aggregate_bias
                from shunkan.intel.feeds import fetch_news

                items = fetch_news(limit=8)
                bias = aggregate_bias(items)
                out["news"] = {
                    "bias_score": getattr(bias, "score", None),
                    "bias_label": getattr(bias, "label", None),
                    "n_items": getattr(bias, "n_items", 0),
                    "headlines": [{"title": i.title, "source": getattr(i, "source", "")}
                                  for i in items[:5]],
                }
            except Exception as exc:
                out["news"] = {"error": f"news unavailable: {str(exc)[:120]}"}
        else:
            try:
                from shunkan.data.newsstore import _read_all

                arch = _read_all()
                if arch.empty:
                    raise DataError("news archive empty")
                arch["ts"] = pd.to_datetime(arch["ts"], utc=True, errors="coerce")
                day_rows = arch[arch["ts"].dt.date == on_date]
                out["news"] = {
                    "bias_score": (float(day_rows["sentiment"].mean())
                                   if len(day_rows) else None),
                    "n_items": int(len(day_rows)),
                    "headlines": [{"title": t, "source": s} for t, s in
                                  zip(day_rows["title"].head(5), day_rows["source"].head(5))],
                    "source": "news archive (as stored that day)",
                }
            except Exception as exc:
                out["news"] = {"error": f"archive news unavailable: {str(exc)[:120]}"}

        out["prov"] = prov(
            "daily analysis",
            {"sections": ", ".join(k for k in out if k not in ("symbol", "prov")),
             "mode": "live" if on_date is None else f"replay of {on_date}"},
            "local archive + chain store + NSE participant archive",
            method="root first: underlying from the daily archive, VIX percentile "
                   "from the 2008+ series, chain analytics from the live source "
                   "(live) or the day's last captured snapshot (replay), "
                   "participant nets from NSE's published file",
            caveat="facts and base rates only, no verdict by design - every "
                   "section fails to a named reason rather than a filled box",
        )
        return out

    @app.get("/api/analysis/daily/{symbol}")
    def analysis_daily(symbol: str, on: str | None = None):
        """The daily analysis. `?on=YYYY-MM-DD` replays a past day: the
        close-of-day JOURNAL when one was recorded (what the terminal
        actually said), else a reconstruction from the stores, labelled.
        """
        from shunkan.analytics.daily import read_journal
        from shunkan.markets import today_ist

        sym = symbol.upper()
        on_date = None
        if on:
            try:
                on_date = datetime.strptime(on, "%Y-%m-%d").date()
            except ValueError as exc:
                raise HTTPException(400, f"on must be YYYY-MM-DD, got {on!r}") from exc
            if on_date > today_ist():
                raise HTTPException(400, f"{on_date} has not happened yet")
            if on_date == today_ist():
                on_date = None      # today = the live composition
        if on_date is not None:
            j = read_journal(sym, on_date)
            if j is not None:
                j["served_from"] = ("journal recorded "
                                    + j.get("_journal", {}).get("recorded_at", "?"))
                return _clean(j)
        return _clean(_compose_daily(sym, on_date))

    @app.get("/api/participants")
    def participants_history(days: int = 250):
        """Participant-wise positioning through time, for the FII/DII view.

        Levels are structural (FII runs net short index futures as a hedge,
        permanently); the CHANGE is what commentary reads. The 4-year screen
        found no next-day directional signal in either - the view carries
        that finding rather than implying the chart predicts something.
        """
        from shunkan.data.participant import latest_with_change, store_path

        path = store_path()
        if not path.exists():
            raise HTTPException(404, "no participant data on disk yet - the "
                                     "6-hourly loop fills it")
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"])
        all_days = sorted(df["date"].unique())
        window = all_days[-max(2, min(days, len(all_days))):]
        df = df[df["date"].isin(window)]
        series: dict = {}
        for who, grp in df.groupby("client_type"):
            g = grp.sort_values("date")
            series[who] = {
                "dates": [pd.Timestamp(d).date().isoformat() for d in g["date"]],
                "idx_fut_net": [int(x) for x in g["idx_fut_net"]],
                "idx_opt_net": [int(x) for x in g["idx_opt_net"]],
            }
        return _clean({
            "days_on_disk": len(all_days),
            "window_days": len(window),
            "as_of": pd.Timestamp(all_days[-1]).date().isoformat(),
            "series": series,
            "latest": latest_with_change(),
            "caveat": ("levels are structural, the change is the read - and "
                       "the 4-year screen (research/DECISIONS.md 2026-08-18) "
                       "found no next-day directional edge in either; this is "
                       "positioning fact, not a signal"),
        })

    _scan_cache: dict = {}

    def _disk_get(key: str, max_age_s: float):
        """Read-through cache that survives restarts. The in-memory dict
        only ever knew about this process; a terminal that forgets a
        400-page annual report on restart is a terminal that refetches it."""
        try:
            from shunkan.store.graph import graph

            return graph().cache_get(key, max_age_s)
        except Exception:
            return None

    def _disk_put(key: str, payload, kind: str = "", ttl_s: float = 21600):
        try:
            from shunkan.store.graph import graph

            graph().cache_put(key, payload, kind, ttl_s)
        except Exception:
            pass

    @app.get("/api/calendar")
    def calendar():
        """The expiry calendar, from the instruments dumps - real listings,
        not a typed-in schedule. Holidays and earnings dates are REFUSED
        with reasons: neither has a source wired yet, and a guessed calendar
        is how someone holds a position into a settlement they mistimed."""
        from shunkan.data.kite_fno import load_instruments

        out_venues = {}
        for venue in ("NFO", "BFO", "MCX", "CDS"):
            try:
                df = load_instruments(exchange=venue)
                exp = df[df["expiry"].astype(str) != ""].copy()
                exp["expiry"] = pd.to_datetime(exp["expiry"], errors="coerce")
                exp = exp.dropna(subset=["expiry"])
                exp = exp[exp["expiry"].dt.date >= datetime.now(IST).date()]
                nxt = (exp.groupby(exp["expiry"].dt.date)
                       .agg(contracts=("tradingsymbol", "count"),
                            names=("name", lambda s: sorted(set(s))[:6]))
                       .reset_index().sort_values("expiry").head(8))
                out_venues[venue] = [
                    {"date": str(r["expiry"]), "contracts": int(r["contracts"]),
                     "names": list(r["names"])}
                    for _, r in nxt.iterrows()
                ]
            except Exception as exc:
                out_venues[venue] = {"error": str(exc)[:120]}
        return _clean({
            "venues": out_venues,
            "holidays": {"error": "no sourced exchange holiday list wired yet - "
                                  "refusing to guess one"},
            "earnings": {"error": "no earnings-date source wired yet - "
                                  "refusing to guess dates money depends on"},
        })

    _own_scan: dict = {"running": False}

    @app.post("/api/ownership/scan")
    def ownership_scan(universe: str = "core", limit: int = 60):
        """Populate the ownership registry across a universe.

        One shareholding filing per company, politely paced. This is what
        turns 'which companies does LIC hold' from a two-company answer
        into a real one - and the coverage number always travels with the
        result, so a partial scan never reads as a full book."""
        import threading
        import time as _time

        if _own_scan.get("running"):
            return {"running": True, **_own_scan}
        if universe not in HEAT_UNIVERSES:
            raise HTTPException(400, f"universe must be one of {sorted(HEAT_UNIVERSES)}")
        try:
            from shunkan.data.constituents import universe as _uni

            symbols = [c.symbol for c in _uni(HEAT_UNIVERSES[universe])][:limit]
        except Exception as exc:
            raise HTTPException(502, f"constituents unavailable: {str(exc)[:110]}") from exc

        _own_scan.update({"running": True, "done": 0, "total": len(symbols),
                          "ok": 0, "failed": 0, "universe": universe})

        def run():
            from shunkan.data.filings import latest_shareholding

            for i, sym in enumerate(symbols, 1):
                try:
                    latest_shareholding(sym)      # persists as a side effect
                    _own_scan["ok"] += 1
                except Exception as exc:
                    _own_scan["failed"] += 1
                    _own_scan["last_error"] = f"{sym}: {str(exc)[:90]}"
                _own_scan["done"] = i
                _time.sleep(0.8)                  # polite to the exchange
            _own_scan["running"] = False
            _own_scan["finished_at"] = _now_iso()

        threading.Thread(target=run, daemon=True).start()
        return {"running": True, **_own_scan}

    @app.get("/api/ownership/scan")
    def ownership_scan_status():
        from shunkan.data.filings import registry_stats

        return _clean({**_own_scan, "registry": registry_stats()})

    # -- knowledge graph ---------------------------------------------------

    @app.get("/api/graph")
    def graph_stats():
        from shunkan.store.graph import graph

        g = graph()
        out = g.stats()
        # Ship the health verdict WITH the counts, always. A truncated
        # database still answers COUNT(*) with a plausible-looking number -
        # 4,374 nodes where there had been 62,220 - so a stats block without a
        # verdict is exactly the reassuring-but-wrong screen this codebase
        # exists to avoid.
        out["health"] = g.health()
        return _clean(out)

    @app.get("/api/graph/health")
    def graph_health(deep: bool = False):
        """Structural check. `deep=true` runs the full integrity_check.

        Deliberately does NOT construct a GraphStore: opening one runs the
        schema script, which raises on a file damaged badly enough to matter,
        so routing this through the store would make the endpoint fail in the
        one case it exists to report.
        """
        from shunkan.store.graph import check_health

        return _clean(check_health(deep=deep))

    @app.post("/api/graph/rebuild")
    def graph_rebuild():
        """Rebuild the graph from every parquet store. Seconds, idempotent."""
        from shunkan.data.ingest import rebuild

        return _clean(rebuild())

    @app.get("/api/graph/resolve")
    def graph_resolve(q: str, kind: str | None = None):
        """The mapping service: any spelling of an entity, one canonical id."""
        from shunkan.store.graph import graph

        g = graph()
        nid = g.resolve(q, kind)
        return _clean({"query": q, "node_id": nid,
                       "node": g.node(nid) if nid else None,
                       "candidates": g.search(q, kind, 12) if not nid else []})

    @app.get("/api/graph/node")
    def graph_node(id: str, rel: str | None = None, limit: int = 200):
        from shunkan.store.graph import graph

        g = graph()
        n = g.node(id)
        if n is None:
            raise HTTPException(404, f"no node {id}")
        nb = g.neighbours(id, rel, "both", limit)
        return _clean({"node": n, "neighbours": [vars(x) for x in nb]})

    @app.get("/api/graph/coheld/{symbol}")
    def graph_coheld(symbol: str, rel: str = "scheme_holds", limit: int = 25):
        """What else do this stock's holders hold - crowding, in one hop."""
        from shunkan.store.graph import graph

        g = graph()
        nid = g.resolve(symbol, "company")
        if not nid:
            raise HTTPException(404, f"{symbol} is not in the graph")
        return _clean({"symbol": symbol.upper(), "rel": rel,
                       "rows": g.co_held(nid, rel, limit)})

    # -- MSCI index review -------------------------------------------------

    @app.post("/api/msci/import")
    def msci_import(source: str | None = None):
        from shunkan.data.msci import import_msci

        try:
            return _clean(import_msci(source))
        except DataError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/msci/changes")
    def msci_changes():
        """Names the local rule engine expects to move at the next review -
        forced passive flow, dated."""
        from shunkan.data.msci import review_changes

        try:
            return _clean(review_changes())
        except DataError as exc:
            raise HTTPException(404, str(exc)) from exc

    # -- mutual funds ------------------------------------------------------

    @app.post("/api/funds/import")
    def funds_import(source: str | None = None):
        """Pull the mfresearch pipeline's stores into Shunkan's own."""
        from shunkan.data.funds import import_from_pipeline

        try:
            return _clean(import_from_pipeline(source))
        except DataError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/funds")
    def funds_stats():
        from shunkan.data.funds import store_stats

        return _clean(store_stats())

    @app.get("/api/funds/search")
    def funds_search(q: str = "", limit: int = 40):
        from shunkan.data.funds import search_schemes

        try:
            return {"rows": search_schemes(q, limit)}
        except DataError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/funds/holders/{symbol}")
    def funds_holders(symbol: str):
        """Which SCHEMES hold this stock - the economic owner behind the
        AMC name that appears in the SEBI shareholding pattern."""
        from shunkan.data.funds import schemes_holding

        try:
            return _clean(schemes_holding(symbol))
        except DataError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/funds/perf/{isin}")
    def funds_perf(isin: str):
        """Returns and risk computed from the fund's own NAV series, with
        its benchmark beside it."""
        from shunkan.data.funds import scheme_performance

        try:
            return _clean(scheme_performance(isin))
        except DataError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/funds/category/{category}")
    def funds_category(category: str, window: str = "1y"):
        """A category ranked on one window - the quartile view."""
        from shunkan.data.funds import category_table

        try:
            return _clean(category_table(category, window))
        except DataError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/funds/{isin}")
    def funds_detail(isin: str):
        from shunkan.data.funds import scheme_detail

        try:
            return _clean(scheme_detail(isin))
        except DataError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/holder")
    def holder_lookup(q: str):
        """Reverse ownership: every company in the local registry this holder
        appears in. Coverage is what has been scanned, and says so."""
        from shunkan.data.filings import holder_positions

        if len(q.strip()) < 3:
            raise HTTPException(400, "query needs at least 3 characters")
        return _clean(holder_positions(q.strip()))

    def _split_sbo(text: str) -> list:
        try:
            from shunkan.store.graph import split_beneficial_owners

            return split_beneficial_owners(text or "")
        except Exception:
            return []

    _supply_builds: dict = {}

    _warm: dict = {"running": False}

    @app.post("/api/company/warm")
    def warm_companies(universe: str = "core", segments: int = 1):
        """Pre-build the structured layers for a whole universe.

        Profile, filings, ownership and segments per company, written to the
        disk cache and the graph, so every company page afterwards opens
        from local storage. Annual reports are deliberately NOT included -
        they are 15 MB each and change once a year, so they stay lazy."""
        import threading
        import time as _time

        if _warm.get("running"):
            return {"running": True, **_warm}
        if universe not in HEAT_UNIVERSES:
            raise HTTPException(400, f"universe must be one of {sorted(HEAT_UNIVERSES)}")
        try:
            from shunkan.data.constituents import universe as _uni

            symbols = [c.symbol for c in _uni(HEAT_UNIVERSES[universe])]
        except Exception as exc:
            raise HTTPException(502, f"constituents unavailable: {str(exc)[:110]}") from exc

        _warm.update({"running": True, "done": 0, "total": len(symbols),
                      "ok": 0, "failed": 0, "universe": universe,
                      "started": _now_iso()})

        def run():
            for i, sym in enumerate(symbols, 1):
                try:
                    company(sym)                     # profile + all filings
                    if segments:
                        company_segments(sym)        # Ind AS 108
                    _warm["ok"] += 1
                except Exception as exc:
                    _warm["failed"] += 1
                    _warm["last_error"] = f"{sym}: {str(exc)[:80]}"
                _warm["done"] = i
                _warm["current"] = sym
                _time.sleep(0.4)
            _warm["running"] = False
            _warm["finished"] = _now_iso()
            try:
                from shunkan.data.ingest import rebuild

                rebuild()
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()
        return {"running": True, **_warm}

    @app.get("/api/company/warm")
    def warm_status():
        return _clean(dict(_warm))

    @app.get("/api/company/{symbol}/segments")
    def company_segments(symbol: str):
        """Ind AS 108 segment reporting from the newest quarterly filing -
        revenue and profit by the businesses the company actually runs."""
        import time as _time

        sym = symbol.upper().replace(".NS", "")
        ck = f"segments:{sym}"
        disk = _disk_get(ck, 7 * 86400)
        if disk is not None:
            return disk
        try:
            from shunkan.data.indas import segments_for

            out = _clean(segments_for(sym))
        except DataError as exc:
            out = {"error": str(exc)[:200], "symbol": sym}
        _disk_put(ck, out, "segments", 7 * 86400)
        return out

    @app.get("/api/company/{symbol}/supply")
    def company_supply(symbol: str, refresh: int = 0):
        """The SPLC map, built from the company's own annual report.

        Bloomberg draws this from licensed supplier/customer data. No such
        feed exists for NSE names, so the map is EVIDENCE RETRIEVAL from
        the filed report: every node carries the sentence it came from and
        nothing is inferred. Downloads and parses a 200-400 page PDF, so it
        builds in the background and the client polls."""
        import time as _time
        import threading

        sym = symbol.upper().replace(".NS", "")
        ck = f"supply:{sym}"
        hit = _scan_cache.get(ck)
        if hit and not refresh and _time.monotonic() - hit[0] < 86400:
            return hit[1]
        if not refresh:
            disk = _disk_get(ck, 7 * 86400)
            if disk is not None:
                _scan_cache[ck] = (_time.monotonic(), disk)
                return disk
        b = _supply_builds.get(sym)
        if b:
            return {"building": True, "stage": b.get("stage", "starting"), "symbol": sym}
        if is_offline():
            raise HTTPException(502, "supply map needs the network")

        _supply_builds[sym] = {"stage": "locating the annual report"}

        def build():
            try:
                from shunkan.data.filings import latest_readable_report
                from shunkan.data.supply_chain import build_supply_map

                _supply_builds[sym]["stage"] = "downloading the annual report"
                # newest READABLE, not newest: see latest_readable_report
                ar, text, pages = latest_readable_report(sym)
                _supply_builds[sym]["stage"] = (
                    f"read FY{ar.get('to_year')} report ({ar.get('size')})")
                _supply_builds[sym]["stage"] = f"reading {pages} pages"
                name = ""
                try:
                    name = (_scan_cache.get(f"company:{sym}", (0, {}))[1]
                            .get("profile", {}).get("name") or "")
                except Exception:
                    pass
                m = build_supply_map(sym, text, ar["url"], pages, name)
                out = _clean({**m.to_dict(),
                              "report_year": f"{ar['from_year']}-{ar['to_year']}",
                              "report_size": ar["size"],
                              "filed": ar.get("filed"),
                              "available_years": [f"{r['from_year']}-{r['to_year']}"
                                                  for r in ars[:12]]})
                _scan_cache[ck] = (_time.monotonic(), out)
                _disk_put(ck, out, "supply", 7 * 86400)
            except Exception as exc:
                _scan_cache[ck] = (_time.monotonic(),
                                   {"error": str(exc)[:200], "symbol": sym})
            finally:
                _supply_builds.pop(sym, None)

        threading.Thread(target=build, daemon=True).start()
        return {"building": True, "stage": "locating the annual report", "symbol": sym}

    # ---- LLM extraction -------------------------------------------------
    # Registered BEFORE /api/company/{symbol}: FastAPI matches in declaration
    # order and the bare path would otherwise swallow /extract as a symbol.
    # That exact bug already cost us /api/candles/scan once.

    _extract_builds: dict = {}

    @app.get("/api/company/{symbol}/extract")
    def company_extract_get(symbol: str):
        """The stored extraction for a symbol, or what it is currently doing."""
        from shunkan.data.llm import load_extraction

        sym = symbol.upper().replace(".NS", "")
        b = _extract_builds.get(sym)
        if b:
            return {"building": True, "stage": b.get("stage", "starting"), "symbol": sym}
        ex = load_extraction(sym)
        if ex is None:
            # "Run one from the ADMIN tab" is the right advice ONLY when a
            # source document exists. For a company that listed weeks ago -
            # Emmvee and PhysicsWallah both IPO'd into this index - no annual
            # report has been filed on either exchange yet, and telling the
            # reader to press a button that cannot succeed is worse than
            # telling them nothing. Name which of the two cases it is.
            reason = "no extraction stored - run one from the ADMIN tab"
            runnable = True
            try:
                from shunkan.data.filings import annual_reports
                annual_reports(sym)
            except Exception:                                  # noqa: BLE001
                try:
                    from shunkan.data.bse import annual_reports as _bse_reports
                    _bse_reports(sym)
                except Exception:                              # noqa: BLE001
                    runnable = False
                    reason = ("neither NSE nor BSE lists an annual report for "
                              f"{sym}. A company that has only just listed has "
                              "not filed one yet, and there is nothing to "
                              "extract from until it does.")
            return {"symbol": sym, "extracted": False, "reason": reason,
                    "runnable": runnable}
        from dataclasses import asdict as _asdict

        out = _asdict(ex)
        out["extracted"] = True
        out["counts"] = ex.counts()
        return out

    @app.post("/api/company/{symbol}/extract")
    def company_extract_run(symbol: str):
        """Run an extraction in the background; the client polls the GET.

        Background because a 500-page download plus a medium-effort call runs
        two to three minutes, which is well past any sane request timeout.
        """
        import threading

        from shunkan.data.llm import extract_company, load_settings

        sym = symbol.upper().replace(".NS", "")
        if _extract_builds.get(sym):
            return {"building": True, "stage": _extract_builds[sym].get("stage"),
                    "symbol": sym}
        s = load_settings()
        if not s.enabled:
            raise HTTPException(400, "LLM extraction is disabled - enable it in ADMIN")
        if is_offline():
            raise HTTPException(502, "extraction needs the network")
        _extract_builds[sym] = {"stage": "queued"}

        def run():
            try:
                extract_company(
                    sym, settings=s,
                    progress=lambda m: _extract_builds.setdefault(sym, {}).update(stage=m))
            except Exception as exc:
                _extract_builds[sym] = {"stage": f"failed: {str(exc)[:180]}"}
                import time as _t
                _t.sleep(20)   # leave the error visible to one poll cycle
            finally:
                _extract_builds.pop(sym, None)

        threading.Thread(target=run, daemon=True).start()
        return {"building": True, "stage": "queued", "symbol": sym}

    @app.get("/api/entity/{symbol}")
    def entity_map(symbol: str, hops: int = 2, top: int = 20):
        """Everything the graph knows about one entity, in one call.

        The company page needs four things that used to be four round trips -
        who it trades with, who it IS related to, what it makes and buys, and
        who owns it. They come from different sources (BSE XBRL, an annual
        report sentence, a mutual-fund disclosure) so each block states its
        own provenance rather than presenting a single undifferentiated blob.
        """
        from shunkan.store.graph import GraphStore

        g = GraphStore()
        # Drilling from a counterparty passes the node id it was drawn with
        # ("company:RELIANCE INTERNATIONAL") rather than a ticker, because a
        # counterparty usually HAS no ticker. Accept both.
        sym = symbol.replace(".NS", "")
        nid = (sym if ":" in sym and g.node(sym)
               else g.resolve(sym.upper(), kind="company")
               or g.resolve(sym.upper()))
        if not nid:
            raise HTTPException(404, f"{sym} is not in the graph")
        sym = sym.upper() if ":" not in sym else sym
        node = g.node(nid) or {}

        trade = g.trade_summary(nid, top=top)
        # The cap is PER RELATION. Counts come from COUNT(*) rather than from
        # len() of the capped list, so a truncated group reports as truncated
        # instead of quietly redefining its own total.
        STRUCT_CAP = 600
        struct = g.structure(nid, limit=STRUCT_CAP)
        true_counts = g.structure_counts(nid)
        by_rel: dict = {}
        for r in struct:
            by_rel.setdefault(r["rel"], []).append(r)
        capped = {rel: true_counts.get(rel, len(v))
                  for rel, v in by_rel.items()
                  if true_counts.get(rel, 0) > len(v)}

        # The verbatim quote lives in edge.meta, which neighbours() drops.
        # Query directly: a supply-chain claim without the sentence that
        # supports it is exactly the kind of unsourced assertion this
        # codebase refuses to render.
        disclosed: dict = {}
        rows = g._con.execute(
            "SELECT e.rel, e.dst id, n.name, e.as_of, e.source, e.meta "
            "FROM edge e JOIN node n ON n.id = e.dst "
            "WHERE e.src = ? AND e.rel IN "
            "('consumes','produces','sells_to','operates') "
            "ORDER BY e.rel, n.name", (nid,)).fetchall()
        for r in rows:
            m = json.loads(r["meta"] or "{}")
            disclosed.setdefault(r["rel"], []).append({
                "id": r["id"], "name": r["name"], "quote": m.get("quote"),
                "match": m.get("match"), "as_of": r["as_of"],
                "source": r["source"]})

        owners = [{"id": n.id, "name": n.name, "pct": n.weight, "as_of": n.as_of,
                   "source": n.source}
                  for n in g.neighbours(nid, rel="holds", direction="in", limit=60)]
        schemes = [{"id": n.id, "name": n.name, "value": n.weight, "as_of": n.as_of}
                   for n in g.neighbours(nid, rel="scheme_holds", direction="in",
                                         limit=40)]

        periods = sorted({p for side in trade.values() for r in side
                          for p in r["periods"] if p and p != "?"})
        return {
            "symbol": sym, "node": nid, "name": node.get("name", sym),
            "trade": trade, "periods": periods,
            "structure": by_rel,
            "structure_counts": {k: true_counts.get(k, len(v))
                                 for k, v in by_rel.items()},
            "structure_capped": capped,
            "structure_cap": STRUCT_CAP,
            "disclosed": disclosed,
            "owners": owners[:40], "schemes": schemes,
            "sources": {
                "trade": "BSE related-party XBRL (SEBI LODR Reg 23(9)) — "
                         "half-yearly, frozen at Sep 2024",
                "structure": "BSE related-party XBRL — relationship as filed",
                "disclosed": "the company's own annual report,every node quoted",
                "owners": "NSE shareholding XBRL / mutual-fund disclosures",
            },
        }

    @app.get("/api/entity/{symbol}/trail")
    def entity_trail(symbol: str, hops: int = 2, max_nodes: int = 300,
                     rels: str = ""):
        """The multi-hop walk, for drawing. Node cap is reported, not hidden."""
        from shunkan.store.graph import GraphStore

        g = GraphStore()
        sym = symbol.replace(".NS", "")
        nid = (sym if ":" in sym and g.node(sym)
               else g.resolve(sym.upper(), kind="company")
               or g.resolve(sym.upper()))
        if not nid:
            raise HTTPException(404, f"{sym} is not in the graph")
        rel_t = tuple(r for r in rels.split(",") if r) or None
        return g.trail(nid, hops=max(1, min(hops, 3)), rels=rel_t,
                       max_nodes=max(20, min(max_nodes, 800)))

    @app.get("/api/admin/llm")
    def admin_llm_get():
        """Settings, provider catalogue and spend. Never returns the key."""
        from dataclasses import asdict as _asdict

        from shunkan.data.llm import (EFFORTS, PROVIDERS, key_fingerprint,
                                      ledger_stats, load_settings, stored_symbols)

        s = load_settings()
        return {
            "settings": _asdict(s),
            "key": key_fingerprint(s.provider),      # "…8e4c" or "" - never the key
            "key_set": bool(key_fingerprint(s.provider)),
            "providers": {k: {"models": v["models"], "env": v["env"],
                              "base_url": v["base_url"]}
                          for k, v in PROVIDERS.items()},
            "efforts": EFFORTS,
            "problems": s.validate(),
            "usage": ledger_stats(),
            "extracted": stored_symbols(),
        }

    @app.post("/api/admin/llm")
    async def admin_llm_set(request: Request):
        """Update settings and/or the API key. The key goes to credentials.json
        at 0600 and never into llm.json, which is world-readable by design."""
        from dataclasses import asdict as _asdict

        from shunkan.data.llm import (key_fingerprint, load_settings,
                                      save_settings, set_api_key)

        body = await request.json()
        key = (body.pop("api_key", "") or "").strip()
        if key:
            set_api_key(key, body.get("provider") or load_settings().provider)
        try:
            s = save_settings(**{k: v for k, v in body.items()})
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "settings": _asdict(s), "key": key_fingerprint(s.provider)}

    @app.post("/api/admin/llm/test")
    def admin_llm_test():
        """Cheap round-trip, so a bad key fails in two seconds rather than
        after a 500-page download."""
        from shunkan.data.llm import test_connection

        try:
            return test_connection()
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}

    @app.get("/api/admin/llm/ledger")
    def admin_llm_ledger(limit: int = 100):
        from shunkan.data.llm import ledger, ledger_stats

        return {"rows": ledger(limit), "stats": ledger_stats()}

    @app.get("/api/company/{symbol}")
    def company(symbol: str):
        """Company intelligence, Bloomberg-DES style, honestly sourced.

        Everything here names where it came from. The two things a
        Bloomberg carries that no free source publishes structured - the
        holder-level registry and the supplier/customer graph - come back
        as REFUSALS with reasons, not as invented arrows. The business
        summary is where the raw-material-to-end-user story lives for most
        names, and it is quoted as the source text, not paraphrased into
        false precision."""
        import time as _time

        sym = symbol.upper().replace(".NS", "")
        ck = f"company:{sym}"
        hit = _scan_cache.get(ck)
        if hit and _time.monotonic() - hit[0] < 3600:
            return hit[1]
        disk = _disk_get(ck, 6 * 3600)
        if disk is not None:
            _scan_cache[ck] = (_time.monotonic(), disk)
            return disk
        if is_offline():
            raise HTTPException(502, "company intelligence needs the network "
                                     "(SHUNKAN_OFFLINE=1 is set)")
        import yfinance as yf

        out: dict = {"symbol": sym}
        t = yf.Ticker(f"{sym}.NS")
        try:
            info = t.info or {}
            if not info.get("longName"):
                raise DataError(f"Yahoo has no profile for {sym}.NS")
        except Exception as exc:
            raise HTTPException(502, f"profile unavailable: {str(exc)[:120]}") from exc

        out["profile"] = _clean({
            "name": info.get("longName"),
            "yahoo_sector": info.get("sector"),
            "yahoo_industry": info.get("industry"),
            "hq": ", ".join(x for x in (info.get("city"), info.get("state"),
                                        info.get("country")) if x),
            "employees": info.get("fullTimeEmployees"),
            "website": info.get("website"),
            "market_cap": info.get("marketCap"),
            "trailing_pe": info.get("trailingPE"),
            "price_to_book": info.get("priceToBook"),
            "dividend_yield_pct": info.get("dividendYield"),
            "beta": info.get("beta"),
            "52w_low": info.get("fiftyTwoWeekLow"),
            "52w_high": info.get("fiftyTwoWeekHigh"),
            "source": "Yahoo Finance company profile",
        })
        out["business"] = {
            "summary": info.get("longBusinessSummary")
                       or "no business description published",
            "source": "Yahoo Finance longBusinessSummary, quoted verbatim",
        }
        out["management"] = {
            "officers": [{"name": o.get("name"), "title": o.get("title"),
                          "age": o.get("age"),
                          "pay": o.get("totalPay")}
                         for o in (info.get("companyOfficers") or [])[:12]],
            "source": "Yahoo Finance companyOfficers",
        }
        # The holder registry: SEBI's quarterly shareholding pattern, filed as
        # XBRL. This view used to refuse a holder table for want of a source -
        # the source turned out to be public all along, so the refusal is
        # retired and the exchange filing is what renders. Yahoo's estimate
        # stays only as the fallback when a filing cannot be read.
        ins = info.get("heldPercentInsiders")
        inst = info.get("heldPercentInstitutions")
        try:
            from shunkan.data.filings import latest_shareholding

            sh = latest_shareholding(sym)
            # NOT `t` - that is the yfinance Ticker this function still needs
            # for the financials below. Shadowing it silently replaced every
            # company's statements with an AttributeError.
            tot = sh.totals
            out["ownership"] = _clean({
                # SEBI's tree: promoter + public = 100, and public CONTAINS
                # the institutions. Serving them as three peers is what put
                # this panel past 100% before.
                "promoter_pct": tot.get("promoter"),
                "public_pct": tot.get("public"),
                "inst_domestic_pct": tot.get("inst_domestic"),
                "inst_foreign_pct": tot.get("inst_foreign"),
                "non_institutions_pct": tot.get("non_institutions"),
                "non_promoter_non_public_pct": tot.get("non_promoter_non_public"),
                "as_of": sh.as_of,
                "total_shares": sh.total_shares,
                "categories": sh.categories,
                "holders": [{"name": h.name, "bucket": h.bucket,
                             "category": h.category, "kind": h.kind,
                             "shares": h.shares, "pct": h.pct,
                             "pledged_pct": h.pledged_pct,
                             "beneficial_owner": h.beneficial_owner,
                             # split the same way the graph splits it, so a
                             # family reads as people rather than one string
                             "beneficial_owners": _split_sbo(h.beneficial_owner)}
                            for h in sh.holders],
                "source": f"NSE shareholding pattern XBRL, {sh.as_of}",
                "source_url": sh.source_url,
                "label_note": ("SEBI LODR Reg 31, filed quarterly. Promoter + "
                               "public sum to 100; the institutional splits sit "
                               "INSIDE public. Beneficial owners come from the "
                               "filing's own SBO declarations (Companies Act s.90)."),
            })
        except Exception as exc:
            out["ownership"] = _clean({
                "promoter_pct": ins * 100 if ins is not None else None,
                "public_pct": ((1 - ins) * 100 if ins is not None else None),
                "inst_domestic_pct": None, "inst_foreign_pct": None,
                "holders": [],
                "source": "Yahoo Finance estimate (exchange filing unavailable)",
                "label_note": f"NSE filing unreadable: {str(exc)[:110]}",
            })
        try:
            fin = t.financials
            rows = {}
            for label, key in (("revenue", "Total Revenue"),
                               ("operating_income", "Operating Income"),
                               ("net_income", "Net Income"),
                               ("ebitda", "EBITDA")):
                if key in fin.index:
                    rows[label] = {str(c.date()): _clean(float(v))
                                   for c, v in fin.loc[key].items() if pd.notna(v)}
            margins = {}
            rev = rows.get("revenue", {})
            for y, r in rev.items():
                ni = rows.get("net_income", {}).get(y)
                if r and ni is not None:
                    margins[y] = round(ni / r * 100, 2)
            out["financials"] = {
                "annual": rows,
                "net_margin_pct": margins,
                "source": "Yahoo Finance annual statements",
                "note": "net margin computed here from the two rows above",
            }
        except Exception as exc:
            out["financials"] = {"error": f"statements unavailable: {str(exc)[:100]}"}

        try:
            from shunkan.data.constituents import universe as _uni

            cons = _uni(("NIFTY500",))
            mine = next((c for c in cons if c.symbol == sym), None)
            if mine is None or not mine.industry:
                raise DataError(f"{sym} not in NIFTY500 list - no NSE industry peers")
            peer_syms = [c.symbol for c in cons
                         if c.industry == mine.industry and c.symbol != sym][:24]
            changes = _bulk_day_change([f"{x}.NS" for x in peer_syms]) if peer_syms else {}
            out["peers"] = {
                "nse_industry": mine.industry,
                "rows": [{"symbol": x,
                          "price": changes.get(x, (None, None))[0],
                          "chg_pct": changes.get(x, (None, None))[1]}
                         for x in peer_syms],
                "source": "NSE NIFTY500 constituent taxonomy + bulk quotes",
            }
        except Exception as exc:
            out["peers"] = {"error": str(exc)[:120]}

        try:
            from shunkan.data.msci import status_for

            out["msci"] = status_for(sym)
        except Exception:
            out["msci"] = None

        # The rest of the compelled record. Each block fails to a named
        # reason on its own, so one dead endpoint cannot blank the page.
        from shunkan.data import filings as _f

        for key, fn in (("insider", lambda: _f.insider_trades(sym, 40)),
                        ("board_meetings", lambda: _f.board_meetings(sym, 12)),
                        ("corporate_actions", lambda: _f.corporate_actions(sym, 15)),
                        ("credit_ratings", lambda: _f.credit_ratings(sym, 10)),
                        ("pledge", lambda: _f.promoter_pledge(sym)),
                        ("quarterly", lambda: _f.quarterly_results(sym, 8)),
                        ("announcements", lambda: _f.announcements(sym, 25))):
            try:
                out[key] = fn()
            except Exception as exc:
                out[key] = {"error": str(exc)[:140]}

        out["supply_chain"] = {
            "hint": "GET /api/company/{symbol}/supply — built from the filed "
                    "annual report, every node quoting its sentence",
        }
        out = _clean(out)
        _scan_cache[ck] = (_time.monotonic(), out)
        _disk_put(ck, out, "company", 6 * 3600)
        return out

    def _maybe_holders_count(t) -> float | None:
        try:
            mh = t.major_holders
            return float(mh.loc["institutionsCount"]["Value"])
        except Exception:
            return None

    HEAT_UNIVERSES = {
        "core":     ("NIFTY50", "BANKNIFTY"),
        "next50":   ("NIFTYNEXT50",),
        "n100":     ("NIFTY100",),
        "n200":     ("NIFTY200",),
        "n500":     ("NIFTY500",),
        "mid150":   ("MIDCAP150",),
        "small250": ("SMALLCAP250",),
    }

    _heat_builds: dict = {}

    def _bulk_day_change(ns_symbols: list[str], progress=None) -> dict:
        """Last close vs previous close for MANY symbols, chunked at 80 per
        Yahoo call - one 500-ticker call hangs on throttling (measured 3min+),
        chunks run ~3s each. Returns {SYM: (price, chg_pct)}."""
        import yfinance as yf

        out: dict = {}
        for i in range(0, len(ns_symbols), 80):
            chunk = ns_symbols[i:i + 80]
            try:
                data = yf.download(chunk, period="5d", interval="1d",
                                   group_by="ticker", threads=True,
                                   progress=False, auto_adjust=False)
            except Exception:
                continue
            for sym in chunk:
                try:
                    closes = data[sym]["Close"].dropna()
                    if len(closes) >= 2:
                        last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
                        out[sym.replace(".NS", "").upper()] = (last, (last / prev - 1) * 100)
                except (KeyError, IndexError, TypeError):
                    continue
            if progress:
                progress(min(i + 80, len(ns_symbols)))
        return out

    @app.get("/api/heatmap")
    def heatmap(universe: str = "core"):
        """Sector-grouped tiles for a chosen NSE universe.

        `core` (NIFTY50+BANKNIFTY) up to `n500` - the full 20-sector
        taxonomy only appears from n500; the core board carries ~15 of 20
        and says nothing about Chemicals, Media, Realty, Textiles or
        Diversified, which is why the selector exists. Equal tiles still:
        cap-weighting needs a cap source this codebase does not carry.
        Cached ~5 minutes per universe."""
        import time as _time

        if universe not in HEAT_UNIVERSES:
            raise HTTPException(400, f"universe must be one of {sorted(HEAT_UNIVERSES)}")
        ck = f"heatmap:{universe}"
        hit = _scan_cache.get(ck)
        if hit and _time.monotonic() - hit[0] < 300:
            return hit[1]
        # Big universes build in the BACKGROUND: a 500-name pull is ~20s of
        # chunked Yahoo calls, and a request that blocks that long reads as
        # a hang. The client polls; the loader has a real job.
        b = _heat_builds.get(universe)
        if b and b.get("status") == "building":
            return {"building": True, "done": b.get("done", 0), "total": b.get("total", 0),
                    "universe": universe}

        import threading

        try:
            from shunkan.data.constituents import industry_map, universe as _uni

            cons = _uni(HEAT_UNIVERSES[universe])
            industry = industry_map(cons)
            symbols = [c.symbol for c in cons]
        except Exception as exc:
            raise HTTPException(502, f"constituents unavailable: {str(exc)[:120]}") from exc

        _heat_builds[universe] = {"status": "building", "done": 0, "total": len(symbols)}

        def build():
            try:
                changes = _bulk_day_change(
                    [f"{s}.NS" for s in symbols],
                    progress=lambda n: _heat_builds[universe].update(done=n))
                tiles = []
                for sym in symbols:
                    px, chg = changes.get(sym, (None, None))
                    tiles.append({"symbol": sym, "sector": industry.get(sym, "OTHER"),
                                  "price": px, "chg_pct": chg})
                priced = sum(1 for x in tiles if x["price"] is not None)
                out = _clean({
                    "tiles": tiles, "n": len(tiles), "priced": priced,
                    "universe": universe,
                    "sectors": len({x["sector"] for x in tiles}),
                    "note": (f"{priced}/{len(tiles)} priced - a dash is a name "
                             "Yahoo did not serve, not a zero; equal tiles, no "
                             "fake cap weights"),
                })
                _scan_cache[ck] = (_time.monotonic(), out)
            finally:
                _heat_builds.pop(universe, None)

        threading.Thread(target=build, daemon=True).start()
        return {"building": True, "done": 0, "total": len(symbols), "universe": universe}

    @app.get("/api/calendar")
    def calendar():
        """The expiry calendar, from the instruments dumps - real listings,
        not a typed-in schedule. Holidays and earnings dates are REFUSED
        with reasons: neither has a source wired yet, and a guessed calendar
        is how someone holds a position into a settlement they mistimed."""
        from shunkan.data.kite_fno import load_instruments

        out_venues = {}
        for venue in ("NFO", "BFO", "MCX", "CDS"):
            try:
                df = load_instruments(exchange=venue)
                exp = df[df["expiry"].astype(str) != ""].copy()
                exp["expiry"] = pd.to_datetime(exp["expiry"], errors="coerce")
                exp = exp.dropna(subset=["expiry"])
                exp = exp[exp["expiry"].dt.date >= datetime.now(IST).date()]
                nxt = (exp.groupby(exp["expiry"].dt.date)
                       .agg(contracts=("tradingsymbol", "count"),
                            names=("name", lambda s: sorted(set(s))[:6]))
                       .reset_index().sort_values("expiry").head(8))
                out_venues[venue] = [
                    {"date": str(r["expiry"]), "contracts": int(r["contracts"]),
                     "names": list(r["names"])}
                    for _, r in nxt.iterrows()
                ]
            except Exception as exc:
                out_venues[venue] = {"error": str(exc)[:120]}
        return _clean({
            "venues": out_venues,
            "holidays": {"error": "no sourced exchange holiday list wired yet - "
                                  "refusing to guess one"},
            "earnings": {"error": "no earnings-date source wired yet - "
                                  "refusing to guess dates money depends on"},
        })

    @app.get("/api/heatmap")
    def heatmap():
        """NIFTY50+BANKNIFTY tiles grouped by NSE's sector taxonomy.

        Equal tiles on purpose: sizing by market cap needs a cap source this
        codebase does not carry, and a guessed weight is a fabricated number
        wearing a layout. Colour is the day's move; the sector header carries
        the sector's mean. Cached ~5 minutes."""
        import time as _time

        from shunkan.screener import run_screen

        hit = _scan_cache.get("heatmap")
        if hit and _time.monotonic() - hit[0] < 300:
            return hit[1]
        try:
            from shunkan.data.constituents import industry_map, universe

            uni = universe()
            industry = industry_map(uni)
            symbols = [c.symbol for c in uni]
        except Exception as exc:
            raise HTTPException(502, f"constituents unavailable: {str(exc)[:120]}") from exc
        res = run_screen(provider, [f"{s}.NS" for s in symbols], [])
        tiles = []
        for sym_ns, row in res.table.iterrows():
            sym = str(sym_ns).replace(".NS", "").upper()
            r1d = row.get("ret_1d")
            r1w = row.get("ret_1w")
            tiles.append({
                "symbol": sym,
                "sector": industry.get(sym, "OTHER"),
                "price": None if pd.isna(row.get("price")) else float(row["price"]),
                "chg_pct": None if r1d is None or pd.isna(r1d) else float(r1d) * 100,
                "ret_1w_pct": None if r1w is None or pd.isna(r1w) else float(r1w) * 100,
            })
        out = _clean({
            "tiles": tiles, "n": len(tiles),
            "note": ("equal tiles - market-cap weighting needs a cap source "
                     "this codebase does not carry; colour is the day move"),
        })
        _scan_cache["heatmap"] = (_time.monotonic(), out)
        return out

    @app.get("/api/oi/{symbol}")
    def oi_multistrike_endpoint(symbol: str):
        """Per-strike OI through today's snapshots - the wall-watch chart."""
        from shunkan.analytics.daily import oi_multistrike

        return _clean(oi_multistrike(symbol.upper()))

    @app.get("/api/option_path/{symbol}")
    def option_path_endpoint(symbol: str, strike: float):
        """One strike's CE/PE premium path through today - the live options
        chart, from the snapshot store."""
        from shunkan.analytics.daily import option_path

        return _clean(option_path(symbol.upper(), strike))

    @app.get("/api/straddle/{symbol}")
    def straddle_endpoint(symbol: str):
        """Today's ATM straddle/strangle premium path, front expiry."""
        from shunkan.analytics.daily import straddle_path

        return _clean(straddle_path(symbol.upper()))

    @app.get("/api/candles/scan")
    def candles_scan():
        """Today's patterns across the NIFTY50+BANKNIFTY universe plus the
        two indices - the signal feed, except every row carries its measured
        record instead of implying the pattern means something by existing.
        Cached ~10 minutes: the answer changes once per session close."""
        import time as _time

        from shunkan.analytics.candles import detect_all, pattern_record
        from shunkan.store.store import STORE_DIR

        hit = _scan_cache.get("scan")
        if hit and _time.monotonic() - hit[0] < 600:
            return hit[1]

        try:
            from shunkan.data.constituents import universe

            symbols = [c.symbol for c in universe()]
        except Exception:
            symbols = []
        rows = []
        scanned = 0
        for sym in ["_NSEI", "_NSEBANK"] + symbols:
            f = STORE_DIR / "history" / f"{sym}.parquet"
            if not f.exists():
                continue
            try:
                df = pd.read_parquet(f).sort_values("date")
            except Exception:
                continue
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
            if len(df) < 60:
                continue
            scanned += 1
            det = detect_all(df)
            if det.empty:
                continue
            latest = df.index[-1]
            todays = det[det.index == latest]
            label = {"_NSEI": "NIFTY", "_NSEBANK": "BANKNIFTY"}.get(sym, sym)
            for _, r in todays.iterrows():
                rec = pattern_record(df, det, r["pattern"])
                rows.append({
                    "symbol": label,
                    "date": latest.date().isoformat(),
                    "close": float(df["close"].iloc[-1]),
                    "chg_pct": float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100,
                    "pattern": r["pattern"], "direction": r["direction"],
                    "record": rec,
                })
        rows.sort(key=lambda x: (x["direction"] == "neutral", -abs(x["chg_pct"])))
        out = _clean({
            "rows": rows, "scanned": scanned,
            "note": ("patterns on the LATEST archived session per symbol; the "
                     "record is that symbol's own history, overlapping windows "
                     "stated in the engine doc; most symbols print nothing "
                     "most days and are absent, not hidden"),
        })
        _scan_cache["scan"] = (_time.monotonic(), out)
        return out

    @app.get("/api/candles/{symbol}")
    def candles_symbol(symbol: str):
        """Recent candle patterns on one symbol, each with the archive's
        verdict on what that pattern has historically been worth THERE."""
        from shunkan.analytics.candles import analyze_candles
        from shunkan.markets import INDEX_ALIASES
        from shunkan.store.store import STORE_DIR

        sym = symbol.upper()
        cands = [sym]
        alias = INDEX_ALIASES.get(sym)
        if alias:
            cands.append(alias.replace("^", "_").replace("=", "_"))
        for c in cands:
            f = STORE_DIR / "history" / f"{c}.parquet"
            if f.exists():
                df = pd.read_parquet(f).sort_values("date")
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")
                out = analyze_candles(df)
                out["symbol"] = sym
                out["as_of"] = df.index[-1].date().isoformat()
                return _clean(out)
        raise HTTPException(404, f"no archived history for {sym}")

    @app.get("/api/analysis/intraday/{symbol}")
    def analysis_intraday(symbol: str):
        """Today's max-pain / wall migration from the day's captured
        snapshots. The T-1 map dies at the open; this is what replaces it."""
        from shunkan.analytics.daily import intraday_migration

        return _clean(intraday_migration(symbol.upper()))

    @app.get("/api/brief/{symbol}")
    def brief(symbol: str):
        """Morning brief — one call composing the whole research loop:
        cues → chain positioning → vol setup → quant reads → news, plus a
        transparent vote table. Every section names its source; degraded
        sources degrade the vote's weight visibly, never silently."""
        from shunkan.analytics.models import attention_analogs, kalman_trend
        from shunkan.analytics.viz import price_fan
        from shunkan.data.chains import get_chain
        from shunkan.derivatives.chain import analyze_chain
        from shunkan.derivatives.ivx import analyze_vol
        from shunkan.intel import aggregate_bias
        from shunkan.intel.feeds import fetch_news

        sym = symbol.upper()
        votes: list[dict] = []

        def vote(name, direction, why, flag=""):
            votes.append({"name": name, "dir": direction, "why": why, "flag": flag})

        out: dict = {"symbol": sym,
                     "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

        # -- global cues -----------------------------------------------------
        cue_syms = ["^GSPC", "^IXIC", "INDIAVIX", "USDINR", "GC=F", "BZ=F", "^TNX"]
        cues = {}
        try:
            for s, q in provider.quotes([sym] + cue_syms).items():
                cues[s] = {"price": q.price, "change_pct": q.change_pct}
        except DataError:
            pass
        out["cues"] = cues
        us = [cues[s]["change_pct"] for s in ("^GSPC", "^IXIC")
              if cues.get(s, {}).get("change_pct") is not None]
        if us:
            avg = sum(us) / len(us)
            d = "bullish" if avg > 0.003 else "bearish" if avg < -0.003 else "neutral"
            vote("global cues", d,
                 f"S&P/NASDAQ averaged {avg * 100:+.2f}% after our close — "
                 "overnight gap pressure" if abs(avg) > 0.003 else
                 f"US flat ({avg * 100:+.2f}%) — no overnight gap pressure")

        # -- chain positioning + vol setup ----------------------------------
        hist = None
        try:
            hist = provider.history(sym, period="1y", interval="1d")
        except DataError:
            pass
        try:
            c = get_chain(sym)
            a = analyze_chain(c)
            model_oi = c.is_model
            out["positioning"] = {
                "source": c.source, "trail": c.source_trail, "model_oi": model_oi,
                "expiry": str(c.expiry), "spot": c.spot,
                "pcr": a.pcr_oi, "max_pain": a.max_pain,
                "support": a.support, "resistance": a.resistance,
                "bias": a.bias, "bias_reason": a.bias_reason,
                "straddle": a.straddle_price,
                "expected_move_pct": a.expected_move_pct,
            }
            vote("positioning", a.bias, a.bias_reason,
                 flag="MODEL OI — verify on the 09:20 live chain" if model_oi else "")
            if hist is not None:
                v = analyze_vol(c, hist)
                s1 = a.atm_iv / math.sqrt(252.0) if a.atm_iv else float("nan")
                out["vol"] = {
                    "atm_iv": v.atm_iv, "rv_cc_21": v.rv_cc_21,
                    "rv_park_21": v.rv_park_21,
                    "iv_premium": v.atm_iv - v.rv_cc_21
                    if not (math.isnan(v.atm_iv) or math.isnan(v.rv_cc_21)) else None,
                    "band_1d": [c.spot * (1 - s1), c.spot * (1 + s1)]
                    if not math.isnan(s1) else None,
                    "iv_source": c.source,
                }
        except (DataError, ValueError) as exc:
            out["positioning"] = {"error": str(exc)}

        # -- quant reads (all real price history) ---------------------------
        if hist is not None:
            col = "Close" if "Close" in hist.columns else "close"
            closes = hist[col].to_numpy()
            try:
                k = kalman_trend(closes)
                ann = k.slope[-1] * 252.0
                d = "bullish" if ann > 0.03 else "bearish" if ann < -0.03 else "neutral"
                vote("kalman trend", d,
                     f"filtered drift {ann * 100:+.1f}%/yr, last surprise z {k.last_z:+.2f}")
                out["kalman"] = {"trend_ann": ann, "last_z": k.last_z}
            except ValueError:
                pass
            try:
                f = price_fan(hist, float(closes[-1]), symbol=sym,
                              horizon_days=5, n_paths=2000)
                d = "bullish" if f.prob_up > 0.55 else "bearish" if f.prob_up < 0.45 else "neutral"
                vote("5d bootstrap fan", d,
                     f"P(up) {f.prob_up * 100:.0f}% over 2000 resampled histories; "
                     f"P5 {f.envelope['p5'][-1]:.0f} / P95 {f.envelope['p95'][-1]:.0f}")
                out["fan"] = {"prob_up": f.prob_up,
                              "p5": float(f.envelope["p5"][-1]),
                              "p50": float(f.envelope["p50"][-1]),
                              "p95": float(f.envelope["p95"][-1])}
            except ValueError:
                pass
            try:
                at = attention_analogs(hist, window=90)
                m = at.analog_fwd_mean
                d = "bullish" if m > 0.005 else "bearish" if m < -0.005 else "neutral"
                vote("analog days", d,
                     f"top-{len(at.top_analogs)} similar days averaged {m * 100:+.2f}% "
                     f"over the next {at.fwd_days} sessions",
                     flag="untrained similarity — setups rhyme, outcomes may not")
                out["analogs"] = {"fwd_mean": m,
                                  "top": at.top_analogs[:3]}
            except ValueError:
                pass

        # -- news ------------------------------------------------------------
        try:
            items = fetch_news(limit=12)
            b = aggregate_bias(items)
            lbl = str(b.label).lower()
            news_dir = ("bullish" if "bull" in lbl
                        else "bearish" if "bear" in lbl else "neutral")
            vote("news bias", news_dir,
                 f"'{b.label}' across {getattr(b, 'n_items', len(items))} headlines, "
                 "6h half-life decay",
                 flag="RSS lags 5–15 min and predates late US moves")
            out["news"] = {"score": b.score, "label": b.label,
                           "gap_call": getattr(b, "gap_call", None),
                           "top_titles": [i.title[:90] for i in items[:4]]}
        except Exception:
            out["news"] = None

        # -- net read: count the votes, say when they disagree ---------------
        score = sum(1 if v["dir"] == "bullish" else -1 if v["dir"] == "bearish" else 0
                    for v in votes)
        n_dir = sum(1 for v in votes if v["dir"] != "neutral")
        disagree = (any(v["dir"] == "bullish" for v in votes)
                    and any(v["dir"] == "bearish" for v in votes))
        if n_dir == 0:
            label = "no signal — everything reads neutral"
        elif disagree and abs(score) <= 1:
            label = "no directional edge — signals conflict; trade the range/levels, not a view"
        elif score >= 2:
            label = "signals lean bullish — but size for the bear case anyway"
        elif score <= -2:
            label = "signals lean bearish — but size for the bull case anyway"
        else:
            label = "weak lean only — one contrary print flips it"
        out["votes"] = votes
        out["net"] = {"score": score, "label": label,
                      "prov": prov(
                          "net = Σ votes (bullish +1, bearish −1, neutral 0)",
                          {"votes cast": len(votes), "directional": n_dir,
                           "disagreement": str(disagree)},
                          "the sections above — each vote names its own source",
                          caveat="a vote count is a summary, not a signal; flagged "
                          "votes (model OI, stale RSS) deserve less weight than "
                          "the count implies. Decision support, not advice.",
                      )}
        return _clean(out)

    @app.get("/api/viz/sabr/{symbol}")
    def viz_sabr(symbol: str, expiry: str | None = None, beta: float = 0.5):
        """Fit SABR to the smile this expiry is actually quoting.

        Returns the parameters, the market-versus-model curve, and the
        residuals. The residuals are the point: a smile the model cannot
        represent has to look wrong on screen rather than look smooth.
        """
        from shunkan.data.chains import get_chain
        from shunkan.derivatives.sabr import calibrate_chain

        want = None
        if expiry:
            try:
                want = datetime.strptime(expiry, "%Y-%m-%d").date()
            except ValueError as exc:
                raise HTTPException(400, f"expiry must be YYYY-MM-DD, got {expiry!r}") from exc
        try:
            c = get_chain(symbol, want) if want else get_chain(symbol)
        except (DataError, ValueError) as exc:
            raise _chain_error(symbol, exc) from exc
        try:
            f = calibrate_chain(c, beta=max(0.0, min(beta, 1.0)))
        except ValueError as exc:
            # Too thin to fit, or a modelled chain. Both are refusals, not
            # errors to paper over with a default surface.
            raise HTTPException(422, str(exc)) from exc

        grid = np.linspace(f.strikes.min(), f.strikes.max(), 120)
        return _clean({
            "symbol": c.symbol, "expiry": str(c.expiry), "forward": f.forward,
            "t_years": f.t_years, "source": c.source, "is_model": c.is_model,
            "params": {"alpha": f.alpha, "beta": f.beta, "rho": f.rho, "nu": f.nu},
            "fit": {"rmse_vol_points": f.rmse_vol_points,
                    "max_error_vol_points": f.max_error_vol_points,
                    "quality": f.quality, "good": f.good,
                    "n_used": f.n_used, "n_available": f.n_available},
            "warnings": f.warnings,
            "quotes": [{"strike": float(k), "market_iv": float(m),
                        "model_iv": float(v), "residual": float(r)}
                       for k, m, v, r in zip(f.strikes, f.market_iv,
                                             f.model_iv, f.residuals)],
            "curve": [{"strike": float(k), "iv": float(v)}
                      for k, v in zip(grid, f.iv(grid))],
            "prov": prov(
                "SABR calibration",
                {"beta": f.beta, "strikes fitted": f"{f.n_used} of {f.n_available}",
                 "RMSE": f"{f.rmse_vol_points:.3f} vol points"},
                c.source,
                method="Hagan 2002 lognormal expansion; alpha/rho/nu by weighted "
                       "least squares on OTM quotes, weighted by open interest; "
                       "beta held fixed",
                caveat="beta and rho are near-degenerate on a single smile, so "
                       "beta is an input and not a fitted result. A 'poor' fit "
                       "means the market smile is not SABR-shaped today, not "
                       "that the quotes are wrong.",
            ),
        })

    @app.get("/api/viz/heston/{symbol}")
    def viz_heston(symbol: str, horizon: int = 120, kappa: float = 2.0,
                   xi: float = 0.6, rho: float = -0.7):
        from shunkan.analytics.models import heston_fan
        from shunkan.data.chains import get_chain
        from shunkan.derivatives.chain import analyze_chain

        try:
            c = get_chain(symbol)
            a = analyze_chain(c)
            h = heston_fan(c.spot, a.atm_iv, horizon=max(20, min(horizon, 250)),
                           kappa=max(0.1, min(kappa, 10.0)),
                           xi=max(0.01, min(xi, 3.0)),
                           rho=max(-0.99, min(rho, 0.99)))
        except (DataError, ValueError) as exc:
            raise HTTPException(502, str(exc)) from exc
        return _clean({
            "symbol": c.symbol, "spot": h.spot, "source": c.source,
            "v0": h.v0, "kappa": h.kappa, "theta": h.theta, "xi": h.xi,
            "rho": h.rho, "feller_ok": h.feller_ok,
            "horizon_days": h.horizon, "n_paths": h.n_paths,
            "days": h.days.tolist(), "paths": h.display_paths.tolist(),
            "vols": h.display_vols.tolist(),
            "envelope": {k: v.tolist() for k, v in h.envelope.items()},
            "terminal_bins": h.terminal_bins.tolist(),
            "terminal_freq": h.terminal_freq.tolist(),
            "prob_up": h.prob_up, "elapsed_ms": h.elapsed_ms,
            "prov": {
                "fan": prov(
                    "dS = μS dt + √V S dW₁ ; dV = κ(θ−V)dt + ξ√V dW₂ ; corr(dW₁,dW₂)=ρ",
                    {"v0 = ATM_IV²": (f"{h.v0:.4f}", f"live ATM IV {a.atm_iv:.1%} · {c.source}"),
                     "κ / θ / ξ / ρ": f"{h.kappa} / {h.theta:.3f} / {h.xi} / {h.rho}",
                     "Feller 2κθ ≥ ξ²": "satisfied" if h.feller_ok else "VIOLATED"},
                    c.source,
                    method="full-truncation Euler, 1/252 steps; v0 anchored to the "
                    "live chain, other parameters are user-set model inputs",
                    caveat="an uncalibrated Heston is a scenario machine, not a "
                    "forecast — fit κ/θ/ξ/ρ to the smile before quoting it",
                ),
            },
        })

    @app.get("/api/viz/kalman/{symbol}")
    def viz_kalman(symbol: str, period: str = "2y", q: float = 1e-5,
                   r: float = 1e-2, start: str | None = None, end: str | None = None):
        from shunkan.analytics.models import kalman_trend

        try:
            hist = _hist(symbol, period=period, start=start, end=end)
            col = "Close" if "Close" in hist.columns else "close"
            k = kalman_trend(hist[col].to_numpy(),
                             q=max(1e-9, min(q, 1.0)), r=max(1e-6, min(r, 1.0)))
        except (DataError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        times = [str(t)[:10] for t in hist.index]
        return _clean({
            "symbol": symbol.upper(), "q": k.q, "r": k.r,
            "times": times,
            "close": hist[col].astype(float).tolist(),
            "level": k.level.tolist(), "slope": k.slope.tolist(),
            "band": k.band.tolist(), "innovation_z": k.innovation_z.tolist(),
            "last_z": k.last_z, "bars": len(times), "elapsed_ms": k.elapsed_ms,
            "prov": {
                "filter": prov(
                    "state [level, slope] on log price; F=[[1,1],[0,1]], observe level",
                    {"process noise q": k.q, "measurement noise r": k.r,
                     "bars": len(times)},
                    f"{period} daily closes via the active provider",
                    method="innovation z = (close − predicted)/√S flags genuine "
                    "surprises; band = ±2σ of level uncertainty",
                    caveat="a filter smooths the past — it does not know the future",
                ),
            },
        })

    @app.get("/api/viz/attention/{symbol}")
    def viz_attention(symbol: str, period: str = "2y", window: int = 90,
                      start: str | None = None, end: str | None = None):
        from shunkan.analytics.models import attention_analogs

        try:
            hist = _hist(symbol, period=period, start=start, end=end)
            a = attention_analogs(hist, window=max(30, min(window, 150)))
        except (DataError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return _clean({
            "symbol": symbol.upper(), "window": a.window,
            "dates": a.dates, "matrix": a.matrix.tolist(),
            "top_analogs": a.top_analogs, "analog_fwd_mean": a.analog_fwd_mean,
            "fwd_days": a.fwd_days, "elapsed_ms": a.elapsed_ms,
            "prov": {
                "attention": prov(
                    "A = softmax(X·Xᵀ/√d) over daily state embeddings "
                    "[ret1, ret5, vol10, RSI, volume-z]",
                    {"window": a.window, "embedding dim": 5,
                     "analog forward window": f"{a.fwd_days} sessions"},
                    "daily closes via the active provider",
                    method="UNTRAINED kernel attention — state similarity, no "
                    "learned weights; the last row = which past days look like today",
                    caveat="analog-days research, not prediction: similar setups "
                    "can resolve in opposite directions",
                ),
            },
        })

    # -- bulk export + local archive --------------------------------------------

    @app.get("/api/export/history")
    def export_history(symbols: str, period: str = "1y", interval: str = "1d",
                       fmt: str = "csv"):
        """Long-format OHLCV export for up to 20 symbols. The source column
        names the provider per row — synthetic offline data is labeled as
        such, never disguised as market data."""
        import io

        from fastapi.responses import Response

        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:20]
        if not syms:
            raise HTTPException(400, "symbols required, comma-separated")
        if fmt not in ("csv", "parquet"):
            raise HTTPException(400, "fmt must be csv or parquet")
        src_name = "synthetic-demo" if is_offline() else (
            getattr(provider, "broker_name", "") or "yahoo/nse")

        frames, failed = [], []
        for s in syms:
            try:
                h = provider.history(s, period=period, interval=interval)
                h = h.rename(columns={c: c.lower() for c in h.columns})
                keep = [c for c in ("open", "high", "low", "close", "volume")
                        if c in h.columns]
                df = h[keep].copy()
                df.insert(0, "timestamp", h.index)
                df.insert(0, "symbol", s)
                df["source"] = src_name
                frames.append(df)
            except (DataError, KeyError, ValueError):
                failed.append(s)
        if not frames:
            raise HTTPException(502, f"No data for any of: {', '.join(syms)}")
        out = __import__("pandas").concat(frames, ignore_index=True)

        stamp_str = datetime.now().strftime("%Y%m%d-%H%M")
        name = f"shunkan-{period}-{interval}-{stamp_str}"
        if fmt == "parquet":
            buf = io.BytesIO()
            out.to_parquet(buf, index=False)
            return Response(buf.getvalue(), media_type="application/octet-stream",
                            headers={"Content-Disposition":
                                     f'attachment; filename="{name}.parquet"',
                                     "X-Failed-Symbols": ",".join(failed)})
        csv = out.to_csv(index=False)
        return Response(csv, media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{name}.csv"',
                                 "X-Failed-Symbols": ",".join(failed)})

    @app.get("/api/store/archive")
    def archive_stats():
        from shunkan.store import HistoryArchive

        return _clean(HistoryArchive().stats())

    backfill_status = {"running": False, "done": 0, "total": 0, "current": "",
                       "ok": 0, "failed": [], "started_at": None, "finished_at": None}

    def _backfill_symbols() -> list[str]:
        """Everything Shunkan knows about: index aliases, pulse boards, the
        Indian universes, the watchlist — plus every F&O underlying from the
        broker instruments dump when a broker is connected."""
        from shunkan.markets import INDEX_ALIASES
        from shunkan.screener import UNIVERSES

        _pb = _pulse_boards()
        syms = set(INDEX_ALIASES) | {r["ticker"] for r in _pb["india"] + _pb["global"]}
        syms |= set(load_watchlist())
        for u in ("nifty50", "banks", "it", "fno"):
            syms |= set(UNIVERSES[u])
        broker = getattr(provider, "broker", None)
        if broker is not None:
            try:
                from shunkan.data.kite_fno import load_instruments

                inst = load_instruments(broker)
                und = inst.loc[inst["instrument_type"] == "FUT", "name"].dropna()
                syms |= {str(n).upper() for n in und.unique() if str(n).strip()}
            except Exception:
                pass
        return sorted(syms)

    @app.post("/api/archive/backfill")
    async def archive_backfill():
        """Max-history backfill of every known symbol into the local archive.
        Runs in the background; poll GET /api/archive/backfill for progress.
        Never runs offline — synthetic data is never written to the store."""
        from shunkan.store import HistoryArchive

        if is_offline():
            raise HTTPException(400, "offline mode — backfill needs live sources "
                                     "(synthetic data is never written to the store)")
        if backfill_status["running"]:
            raise HTTPException(409, "backfill already running")

        symbols = _backfill_symbols()
        backfill_status.update(running=True, done=0, total=len(symbols), ok=0,
                               failed=[], current="",
                               started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               finished_at=None)
        src = (getattr(provider, "broker_name", "") or "yahoo/nse")
        archive = HistoryArchive()

        def _pull(sym: str):
            h = provider.history(sym, period="max", interval="1d")
            archive.upsert(sym, h, src)

        async def job():
            for sym in symbols:
                backfill_status["current"] = sym
                try:
                    await asyncio.to_thread(_pull, sym)
                    backfill_status["ok"] += 1
                except Exception:
                    if len(backfill_status["failed"]) < 40:
                        backfill_status["failed"].append(sym)
                backfill_status["done"] += 1
                await asyncio.sleep(0.35)  # stay polite with the sources
            backfill_status.update(running=False, current="",
                                   finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

        asyncio.create_task(job())
        return {"started": True, "total": len(symbols)}

    @app.get("/api/archive/backfill")
    def archive_backfill_status():
        return _clean(backfill_status)

    # -- shun script + ml studio -------------------------------------------------

    @app.post("/api/script/run")
    def script_run(req: ScriptRequest):
        from shunkan.backtest import BacktestConfig, run_backtest
        from shunkan.script import ScriptError, run_script

        try:
            hist = provider.history(req.symbol, period=req.period, interval=req.interval)
        except DataError as exc:
            raise HTTPException(502, str(exc)) from exc
        try:
            r = run_script(req.code, hist)
        except ScriptError as exc:
            return _clean({"ok": False, "error": str(exc), "line": exc.line})

        out = {
            "ok": True, "symbol": req.symbol.upper(),
            "plots": [{"title": p["title"], "color": p["color"], "panel": p["panel"],
                       "points": _series(p["values"].dropna())} for p in r.plots],
            "hlines": r.hlines, "variables": r.variables,
            "elapsed_ms": r.elapsed_ms, "bars": len(hist),
        }
        if r.signal is not None:
            bt = run_backtest(hist, r.signal, BacktestConfig(),
                              symbol=req.symbol, strategy_name="shun-script")
            pos = r.signal
            flips = pos[pos.diff().fillna(0) != 0]
            out["markers"] = [
                {"time": str(t)[:10], "dir": int(v)} for t, v in flips.items()
            ][-120:]
            out["backtest"] = {
                "metrics": bt.metrics(),
                "equity": _series(bt.equity / bt.initial_cash),
                "prov": prov(
                    "same engine as BTL: next-bar fills, 5bps+5bps costs per side",
                    {"bars": len(hist), "signal changes": int((pos.diff() != 0).sum())},
                    "Shunkan backtest engine on the script's target-position series",
                    caveat="a script tuned until the curve looks good is curve-fit — "
                    "validate out-of-sample before believing it",
                ),
            }
        return _clean(out)

    @app.get("/api/ml/features")
    def ml_features():
        from shunkan.ml import FEATURES

        return {name: desc for name, (_, desc) in FEATURES.items()}

    @app.post("/api/ml/train")
    def ml_train(req: MLTrainRequest):
        from shunkan.ml import train_model

        try:
            hist = _hist(req.symbol, period=req.period, start=req.start, end=req.end)
            r = train_model(hist, req.features, model=req.model,
                            horizon=req.horizon, test_split=req.test_split)
        except (DataError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        edge = r.acc_test - r.baseline_test
        return _clean({
            "model": r.model, "symbol": req.symbol.upper(), "features": r.features,
            "horizon": r.horizon, "n_train": r.n_train, "n_test": r.n_test,
            "acc_train": r.acc_train, "acc_test": r.acc_test,
            "baseline_test": r.baseline_test, "edge": edge,
            "up_ret_test": r.up_ret_test, "down_ret_test": r.down_ret_test,
            "importances": r.importances,
            "equity_model": r.equity_model.tolist(),
            "equity_bh": r.equity_bh.tolist(),
            "test_index": r.test_index,
            "elapsed_ms": r.elapsed_ms,
            "prov": {
                "accuracy": prov(
                    "direction accuracy on a strictly chronological test split",
                    {"train rows": r.n_train, "test rows": r.n_test,
                     "majority baseline": f"{r.baseline_test:.1%}",
                     "horizon": f"{r.horizon} sessions"},
                    f"{req.period} daily history via the active provider",
                    method="ridge: closed-form on standardized features; stumps: "
                    "gradient-boosted depth-1 trees on the logistic loss (pure numpy)",
                    caveat="an edge this small on one split is fragile — markets are "
                    "non-stationary and test-set luck is real; treat as exploration, "
                    "not a trading system",
                ),
            },
        })

    @app.post("/api/swarm")
    def swarm(req: SwarmRequest):
        from shunkan.backtest import get_strategy, swarm_optimize

        try:
            strat = get_strategy(req.strategy)
            hist = _hist(req.symbol, period=req.period, start=req.start, end=req.end)
            res = swarm_optimize(
                hist, strat, symbol=req.symbol,
                n_particles=max(8, min(req.particles, 40)),
                n_iters=max(5, min(req.iters, 60)),
            )
        except (DataError, KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        # A swarm is a search, so anything it produces has to be read against
        # how many candidates it tried. Surfacing the count here means the
        # winner cannot be quoted later as if it were a single hypothesis.
        from shunkan.backtest.validate import trials_of

        t = trials_of(res)
        return _clean({
            "symbol": res.symbol, "strategy": res.strategy,
            "trials": {"n": t.n_trials, "sharpe_std": t.sharpe_std,
                       "source": t.source},
            "param_names": list(res.param_names),
            "bounds": [list(res.bounds[0]), list(res.bounds[1])],
            "landscape": {"x": res.landscape_x.tolist(),
                          "y": res.landscape_y.tolist(),
                          "z": res.landscape_z.tolist()},
            "iterations": [
                {"p": it.positions.tolist(), "f": it.fitness.tolist(),
                 "g": it.gbest.tolist(), "gf": it.gbest_fitness}
                for it in res.iterations
            ],
            "best_params": res.best_params, "best_fitness": res.best_fitness,
            "best_metrics": res.best_metrics, "verdict": res.verdict(),
            "n_evals": res.n_evals, "bars": len(hist),
            "elapsed_ms": res.elapsed_ms,
            "prov": {
                "fitness": prov(
                    "fitness(params) = annualised Sharpe of a full vectorized "
                    "backtest (next-bar fills, costs included)",
                    {"unique backtests": res.n_evals,
                     "bars per backtest": (len(hist), f"{req.period} daily history"),
                     "particles × iterations":
                         f"{len(res.iterations[0].fitness)} × {len(res.iterations)}"},
                    "Shunkan backtest engine",
                    method="canonical PSO — inertia 0.72, cognitive/social 1.49, "
                    "reflecting bounds; integer parameters memoized so the swarm "
                    "and the landscape share one evaluation cache",
                    caveat="an optimized Sharpe is an in-sample number — validate "
                    "with walk-forward before believing it",
                ),
            },
        })

    # -- screener / watchlist / portfolio / alerts -------------------------------

    @app.get("/api/screen")
    def screen(universe: str, rules: str = ""):
        from shunkan.screener import UNIVERSES, run_screen

        uni = UNIVERSES.get(universe.lower())
        if uni is None:
            raise HTTPException(400, f"Unknown universe. Choices: {', '.join(UNIVERSES)}")
        rule_list = [r for r in rules.split(",") if r.strip()]
        try:
            result = run_screen(provider, uni, rule_list)
        except (ValueError, DataError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return _clean({
            "rows": [{"symbol": str(s), **{k: _clean(v) for k, v in row.items()}}
                     for s, row in result.table.iterrows()],
            "universe_size": len(result.universe),
            "errors": len(result.errors),
        })

    @app.get("/api/watchlist")
    def get_watchlist():
        return {"symbols": load_watchlist()}

    @app.post("/api/watchlist")
    def set_watchlist(body: dict):
        symbols = [str(s).upper() for s in body.get("symbols", []) if s]
        if not symbols:
            raise HTTPException(400, "symbols required")
        save_watchlist(symbols)
        return {"symbols": load_watchlist()}

    @app.get("/api/portfolio")
    def get_portfolio():
        # Position keys are venue-qualified ("NSE:RELIANCE"), which is not what
        # a price source is asked for. Quote by the instrument's own quotable
        # name, then re-key by position so valuation lines up.
        wanted = {key: p.instrument.quote_symbol
                  for key, p in portfolio.positions.items()
                  if p.instrument.quote_symbol}
        prices = {}
        if wanted:
            try:
                quoted = {s.upper(): q.price
                          for s, q in provider.quotes(list(set(wanted.values()))).items()}
                prices = {key: quoted[sym.upper()]
                          for key, sym in wanted.items() if sym.upper() in quoted}
            except DataError:
                prices = {}

        # Options mark off their own chain, which is also what the Greeks are
        # computed from — one fetch per (underlying, expiry) the book holds.
        from shunkan.data.chains import get_chain
        from shunkan.portfolio.risk import book_greeks, describe

        chains: dict[tuple[str, str], object] = {}
        for p in portfolio.positions.values():
            inst = p.instrument
            if not inst.is_option:
                continue
            ckey = (inst.symbol, str(inst.expiry))
            if ckey in chains:
                continue
            try:
                chains[ckey] = get_chain(inst.symbol, inst.expiry)
            except (DataError, ValueError):
                continue  # unmarkable: named by book_greeks, never zero-filled
        for key, p in portfolio.positions.items():
            inst = p.instrument
            c = chains.get((inst.symbol, str(inst.expiry))) if inst.is_option else None
            if c is None:
                continue
            i = int(np.argmin(np.abs(c.strikes - inst.strike)))
            if abs(float(c.strikes[i]) - inst.strike) < 1e-6:
                prices[key] = float(c.call_ltp[i] if inst.kind == "CE" else c.put_ltp[i])

        risk = book_greeks(portfolio.positions.values(), chains)
        risk["summary"] = describe(risk["net"])
        return _clean({
            "cash": portfolio.cash,
            "realized_pnl": portfolio.realized_pnl,
            "market_value": portfolio.market_value(prices),
            "equity": portfolio.total_equity(prices),
            "unrealized_pnl": portfolio.unrealized_pnl(prices),
            # null when unpriced or priced against a different book — margin
            # nets across legs, so there is no honest per-position figure
            # The oldest source timestamp among the chains this book marked
            # against. The book is only as fresh as its stalest leg.
            "as_of": min((c.as_of for c in chains.values() if c.as_of),
                         default=None),
            "margin_used": portfolio.margin_used(),
            "margin": portfolio.margin,
            # ...and why it is null when it is: never asked, asked and refused,
            # priced against a book that has since changed, or missing a leg
            # the exchange has no contract name for. A dash with no cause is
            # the one thing the tile is not allowed to render.
            "margin_status": portfolio.margin_status(),
            # net delta/gamma/theta/vega across the book. `complete` is false
            # when a leg could not be marked — those legs are named, and the
            # net below excludes them rather than counting them as zero.
            "risk": risk,
            "positions": [
                {"symbol": s, "label": p.instrument.label,
                 "kind": p.instrument.kind,
                 "expiry": str(p.instrument.expiry) if p.instrument.expiry else None,
                 "strike": p.instrument.strike,
                 "lot_size": p.instrument.lot_size,
                 "quantity": p.quantity, "is_short": p.is_short,
                 "avg_cost": p.avg_cost,
                 "last": prices.get(s, p.avg_cost),
                 "market_value": p.market_value(prices.get(s, p.avg_cost)),
                 "unrealized": p.unrealized_pnl(prices.get(s, p.avg_cost)),
                 "expired": p.instrument.expired(),
                 # `expired` is the date-level question the book asks about
                 # membership; `settleable` is the 15:30 bell. They differ only
                 # between the bell and midnight on expiry day — which is when
                 # a settlement price first becomes knowable, so the SETTLE
                 # control follows this flag, not the tag.
                 "settleable": p.instrument.settled()}
                for s, p in sorted(portfolio.positions.items())
            ],
            "history": portfolio.history[-50:],
        })

    @app.post("/api/portfolio/trade")
    def trade(req: TradeRequest):
        from shunkan.portfolio import Instrument

        side = req.side.strip().upper()
        if side not in ("BUY", "SELL"):
            # Anything-but-buy used to mean sell, which silently opened a short
            # on a typo now that shorts are representable.
            raise HTTPException(400, f"side must be BUY or SELL, got {req.side!r}")

        try:
            expiry = (datetime.strptime(req.expiry, "%Y-%m-%d").date()
                      if req.expiry else None)
        except ValueError as exc:
            raise HTTPException(400, f"expiry must be YYYY-MM-DD, got {req.expiry!r}") from exc
        try:
            inst = Instrument(symbol=req.symbol, kind=req.kind, expiry=expiry,
                              strike=req.strike, lot_size=req.lot_size,
                              exchange=req.exchange or "")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        # Order-in-lots venues: the economic multiplier comes from the sourced
        # spec table, never from the request — Kite's dump says 1 there, and a
        # client repeating that 1 as if it were a lot would book a 1-crore
        # gold contract as one lakh. A client sending a DIFFERENT number gets
        # a refusal naming both, not a silent override.
        lot_src = ""
        if inst.exchange in ORDER_IN_LOTS_VENUES:
            lot, lot_src = economic_lot_size(inst.exchange, inst.symbol, None)
            if req.lot_size not in (None, 1, lot):
                raise HTTPException(400, (
                    f"lot_size {req.lot_size} conflicts with the sourced "
                    f"{inst.exchange} multiplier for {inst.symbol}: {lot} "
                    f"({lot_src})"))
            inst = dataclasses.replace(inst, lot_size=lot)

        # Size: lots when the contract's lot is known, else explicit units.
        if req.lots is not None:
            if not inst.lot_size:
                raise HTTPException(400, (
                    f"No lot size for {inst.label} — cannot size in lots. "
                    + (lot_src or "Send an explicit quantity, or reconnect "
                       "a source that names the lot.")))
            quantity = req.lots * inst.lot_size
        elif req.quantity is not None:
            quantity = req.quantity
        else:
            raise HTTPException(400, "send either lots or quantity")

        price = req.price
        if price is None:
            if inst.quote_symbol is None:
                raise HTTPException(400, (
                    f"{inst.label} has no generic quote — send the traded price "
                    "(the chain row carries it)."))
            try:
                price = provider.quote(inst.quote_symbol).price
            except DataError as exc:
                raise HTTPException(502, str(exc)) from exc

        try:
            if side == "BUY" and not inst.derivative:
                portfolio.buy(inst.symbol, quantity, price)   # cash check on equity
                realized = 0.0
            else:
                realized = portfolio.trade(inst, side, quantity, price)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        portfolio.save()
        return {"ok": True, "price": price, "quantity": quantity,
                "instrument": inst.key, "label": inst.label, "realized": realized}

    @app.post("/api/basket/margin")
    def price_staged_basket(body: dict):
        """Exchange-priced margin for a STAGED basket - legs that are not in
        the book yet. This is the number that decides whether an idea fits
        the account before any of it becomes a position. Live Kite only:
        SPAN netting cannot be approximated locally without lying."""
        from shunkan.data.brokers import KiteProvider, get_broker
        from shunkan.data.contract_specs import ORDER_IN_LOTS_VENUES, economic_lot_size
        from shunkan.data.kite_fno import basket_margin, cached_lot_size
        from shunkan.portfolio import Instrument

        legs_in = body.get("legs") or []
        if not legs_in or len(legs_in) > 20:
            raise HTTPException(400, "between 1 and 20 legs")
        try:
            broker = get_broker()
            if not isinstance(broker, KiteProvider):
                raise DataError("margin pricing needs a live Kite session")
        except DataError as exc:
            raise HTTPException(502, str(exc)) from exc

        legs = []
        for leg in legs_in:
            try:
                expiry = (datetime.strptime(leg["expiry"], "%Y-%m-%d").date()
                          if leg.get("expiry") else None)
                inst = Instrument(symbol=str(leg["symbol"]).upper(),
                                  kind=leg.get("kind", "EQ"),
                                  expiry=expiry,
                                  strike=leg.get("strike"),
                                  exchange=leg.get("exchange") or "")
            except (KeyError, ValueError) as exc:
                raise HTTPException(400, f"bad leg: {exc}") from exc
            lots = int(leg.get("lots") or 0)
            qty = float(leg.get("quantity") or 0)
            if lots and inst.derivative:
                if inst.exchange in ORDER_IN_LOTS_VENUES:
                    lot, src = economic_lot_size(inst.exchange, inst.symbol, None)
                else:
                    lot, src = cached_lot_size(inst.symbol, inst.expiry)
                if not lot:
                    raise HTTPException(400, f"{inst.label}: no lot size ({src})")
                qty = lots * lot
            if qty <= 0:
                raise HTTPException(400, f"{inst.label}: needs lots or quantity")
            legs.append({"instrument": inst,
                         "side": str(leg.get("side", "BUY")).upper(),
                         "quantity": qty})
        try:
            return _clean(basket_margin(broker, legs))
        except DataError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/portfolio/margin")
    def price_book_margin(force: bool = False):
        """Price the whole book against the exchange's own SPAN calculator.

        Deliberately NOT wired into /api/portfolio/trade. A four-leg condor is
        entered one fill at a time, so pricing per fill spends four broker
        round trips on three baskets the trader never meant to hold — and adds
        that latency to every fill. Margin is asked for once per BOOK STATE
        instead, which is exactly what Portfolio.price_margin is idempotent on:
        the view may call this on every draw, and a desk adjusting all day
        still pays one round trip per adjustment. force=true is the explicit
        re-ask, the only way past that memo.

        Always 200. "The exchange would not price this" is an ordinary state
        the tile renders as a dash plus a reason, not a failed request — a 502
        here would turn a normal Tuesday without a broker into an error toast
        on every draw.
        """
        def _answer(status: dict | None = None) -> dict:
            # Same shape as /api/portfolio's margin keys, so one renderer reads
            # both and the two can never drift into disagreeing vocabularies.
            return {"margin_used": portfolio.margin_used(),
                    "margin": portfolio.margin,
                    "margin_status": status or portfolio.margin_status()}

        if not portfolio.positions:
            return _answer()
        if is_offline():
            return _answer({"state": "refused",
                            "reason": "offline mode (SHUNKAN_OFFLINE=1) — no "
                                      "exchange to ask"})
        broker = None
        try:
            from shunkan.data.brokers import KiteProvider, get_broker

            b = get_broker()
            if isinstance(b, KiteProvider):
                broker = b
        except Exception:
            broker = None
        if broker is None:
            # The basket endpoint is the only SPAN calculator here. A local
            # approximation of exchange netting would overstate a hedged book
            # and understate a naked one — precisely the plausible-looking
            # number this terminal refuses to print.
            return _answer({"state": "refused",
                            "reason": "no Kite connection — SPAN margin is the "
                                      "exchange's number or nothing"})
        portfolio.price_margin(broker, force=force)
        return _answer()

    @app.post("/api/portfolio/settle")
    def settle_position(req: SettleRequest):
        """Resolve an expired contract at a price the trader states.

        The price is never derived here. An index option settles against the
        exchange's published closing value of the underlying, and inferring one
        from whatever spot happened to be cached would put a fabricated cash
        movement into realized P&L. The book takes the trader's number,
        journals it as asserted rather than executed, and closes the contract.
        """
        key = req.symbol.strip().upper()
        pos = portfolio.positions.get(key)
        if pos is None:
            raise HTTPException(404, f"No open position keyed {req.symbol!r}")
        inst = pos.instrument
        quantity = pos.net_quantity  # the book drops the position on a full close
        try:
            realized = portfolio.settle_expired(inst, req.price)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        portfolio.save()
        return {"ok": True, "instrument": inst.key, "label": inst.label,
                "quantity": quantity, "price": req.price, "realized": realized}

    @app.get("/api/alerts")
    def get_alerts():
        return {"alerts": [
            {"index": i + 1, "symbol": a.symbol, "metric": a.metric, "op": a.op,
             "value": a.value, "armed": a.armed, "fired_at": a.fired_at,
             "fired_value": a.fired_value, "text": a.describe()}
            for i, a in enumerate(alert_book.alerts)
        ]}

    @app.post("/api/alerts")
    def add_alert(req: AlertRequest):
        try:
            alert = parse_alert(req.rule)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        alert_book.add(alert)
        return {"ok": True, "text": alert.describe()}

    @app.delete("/api/alerts/{index}")
    def delete_alert(index: int):
        try:
            gone = alert_book.remove(index - 1)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "text": gone.describe()}

    def _layouts_path():
        from shunkan.config import APP_DIR

        return APP_DIR / "layouts.json"

    def _load_layouts() -> dict:
        """Named workspace layouts. The old single layout.json becomes
        'main' on first read, so nobody's arrangement is lost to the
        upgrade."""
        from shunkan.config import APP_DIR

        path = _layouts_path()
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        legacy = APP_DIR / "layout.json"
        if legacy.exists():
            try:
                return {"main": json.loads(legacy.text() if False else legacy.read_text())}
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    @app.get("/api/layouts")
    def list_layouts():
        return {"names": sorted(_load_layouts().keys()) or ["main"]}

    @app.get("/api/layout")
    def get_layout(name: str = "main"):
        return _load_layouts().get(name) or {"widgets": None}

    @app.post("/api/layout")
    def save_layout(body: dict, name: str = "main"):
        from shunkan.config import ensure_dirs

        ensure_dirs()
        all_ = _load_layouts()
        all_[name] = body
        _layouts_path().write_text(json.dumps(all_, indent=2))
        return {"ok": True, "name": name}

    @app.delete("/api/layout")
    def delete_layout(name: str):
        all_ = _load_layouts()
        if name in all_ and len(all_) > 1:
            del all_[name]
            _layouts_path().write_text(json.dumps(all_, indent=2))
            return {"ok": True}
        raise HTTPException(400, "cannot delete the last layout")

    @app.get("/api/commodities")
    def commodities():
        """The MCX strip: front GOLD/SILVER/CRUDEOIL futures, live rupee
        prices via Kite. Refuses without a live broker - Yahoo's dollar
        quotes are a different instrument and would not be labelled MCX."""
        from datetime import date as _date

        from shunkan.data.brokers import KiteProvider, get_broker
        from shunkan.data.kite_fno import load_instruments
        from shunkan.stream.factory import front_future_rows

        try:
            broker = get_broker()
            if not isinstance(broker, KiteProvider):
                raise DataError("no Kite session")
            nfo = load_instruments(broker, "MCX")
            fronts = front_future_rows(nfo, ("GOLD", "SILVER", "CRUDEOIL"), _date.today())
            token_to_name = {t: n for t, n in fronts}
            rows = nfo[nfo["instrument_token"].isin(token_to_name)][
                ["instrument_token", "tradingsymbol"]]
            keys = [f"MCX:{ts}" for ts in rows["tradingsymbol"]]
            q = broker.get_json("/quote?" + "&".join(f"i={k}" for k in keys))["data"]
            out = []
            for _, r in rows.iterrows():
                d = q.get(f"MCX:{r['tradingsymbol']}") or {}
                ltp = d.get("last_price")
                prev = (d.get("ohlc") or {}).get("close")
                out.append({
                    "name": token_to_name[int(r["instrument_token"])].replace("FUT", ""),
                    "tradingsymbol": r["tradingsymbol"],
                    "ltp": ltp,
                    "chg_pct": ((ltp / prev - 1) * 100 if ltp and prev else None),
                })
            return _clean({"rows": out, "source": "MCX front futures via Kite"})
        except Exception as exc:
            raise HTTPException(502, f"commodity strip needs a live Kite session: {str(exc)[:100]}") from exc

    @app.get("/api/store/stats")
    def get_store_stats():
        from shunkan.store import store_stats

        return _clean(store_stats())

    @app.get("/api/store/bars/{symbol}")
    def get_store_bars(symbol: str):
        """Today's locally captured 1-minute bars (from the live tick feed)."""
        from shunkan.store import TickStore

        df = TickStore().read_bars(symbol.upper())
        if df is None or df.empty:
            return {"symbol": symbol.upper(), "bars": [],
                    "note": "no locally captured bars for today"}
        return {
            "symbol": symbol.upper(),
            "bars": [
                {"time": int(row.minute * 60), "open": row.open, "high": row.high,
                 "low": row.low, "close": row.close, "volume": row.volume}
                for row in df.itertuples()
            ],
            "note": "built from the live Kite tick stream, stored locally",
        }

    # -- websocket tick feed -------------------------------------------------

    @app.websocket("/ws/ticks")
    async def ws_ticks(ws: WebSocket):
        # Starlette's HTTP middleware never sees a websocket scope, so this
        # route has to repeat the guard itself. It carries the live exchange
        # tape, which is the data Zerodha licenses for your own use only.
        if allowed_hosts:
            host = (ws.headers.get("host") or "").split(":")[0]
            if host not in allowed_hosts:
                await ws.close(code=1008)
                return
        if access_token and not _same_secret(ws.query_params.get("t", ""), access_token):
            await ws.close(code=1008)
            return
        await ws.accept()
        client = await hub.attach(ws)
        try:
            while True:
                # Clients may send sub/unsub ops; silence is also fine.
                hub.handle_op(client, await ws.receive_text())
        except WebSocketDisconnect:
            pass
        finally:
            hub.detach(ws)

    # -- static frontend --------------------------------------------------------

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        def index():
            return FileResponse(STATIC_DIR / "index.html")

    return app


def _atm_iv_for_prov(chain) -> float:
    import numpy as _np

    i = chain.atm_index
    ivs = [v for v in (chain.call_iv[i], chain.put_iv[i]) if not _np.isnan(v)]
    return float(_np.mean(ivs)) if ivs else 0.15


def _load_chart_configs() -> dict:
    from shunkan.config import APP_DIR

    path = APP_DIR / "chart_configs.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _pip_size(symbol: str) -> float:
    """Best-effort pip size for 'pips'-mode stops. NSE points default to 1.0."""
    s = symbol.upper()
    if "JPY" in s:
        return 0.01
    if "=X" in s or (len(s) == 6 and s.isalpha()):  # forex pairs like EURUSD
        return 0.0001
    return 1.0  # NSE equities/indices and everything else: one point


def _exec_summary(cfg) -> dict:
    def leg(mode, value):
        if mode == "none" or value <= 0:
            return "off"
        unit = {"percent": "%", "pips": " pips", "atr": "× ATR"}.get(mode, "")
        return f"{value:g}{unit}" + (" (trailing)" if cfg.trailing and mode != "none" else "")
    filters = []
    if cfg.session_start and cfg.session_end:
        filters.append(f"session {cfg.session_start}-{cfg.session_end}")
    if cfg.atr_min is not None or cfg.atr_max is not None:
        filters.append(f"ATR {cfg.atr_min or 0}-{cfg.atr_max if cfg.atr_max is not None else '∞'}")
    if cfg.cooldown_bars:
        filters.append(f"{cfg.cooldown_bars}-bar cooldown")
    return {"stop": leg(cfg.sl_mode, cfg.sl_value),
            "target": leg(cfg.tp_mode, cfg.tp_value),
            "filters": ", ".join(filters) or "none"}


def _exit_breakdown(trades) -> dict:
    out: dict[str, int] = {}
    for t in trades:
        out[t.exit_reason] = out.get(t.exit_reason, 0) + 1
    return out


def _series(series) -> list[dict]:
    ts = [int(t.timestamp()) for t in series.index]
    vals = series.tolist()
    return [{"time": ts[i], "value": _clean(float(vals[i]))} for i in range(len(ts))]


def _band(index, values) -> list[dict]:
    ts = [int(t.timestamp()) for t in index]
    return [{"time": ts[i], "value": _clean(float(values[i]))} for i in range(len(values))]


class TickHub:
    """Websocket adapter over the TickBus (see shunkan.stream.bus).

    The hub owns feed lifecycle (lazy start on first client, stop on last)
    and one DRAIN TASK per websocket — the only writer to that socket, so
    tick frames and op acks never interleave mid-frame. Everything about who
    receives which symbol lives in the bus, where it is unit-tested without
    a server."""

    def __init__(self) -> None:
        from shunkan.store import BarBuilder

        self.loop: asyncio.AbstractEventLoop | None = None
        self.feed = None
        self.bus = None
        self._base: list[str] = []       # the watchlist as built, pre-dynamic
        self._senders: dict = {}         # ws -> (BusClient, drain task)
        self.bars = BarBuilder()         # live ticks -> 1-min bars -> parquet
        # While True, the feed outlives its websocket clients: the keepalive
        # loop holds it open through market hours so the 1-minute bar archive
        # does not depend on somebody keeping a browser tab open. The
        # recorder should not need an audience.
        self.keepalive = False

    async def ensure_feed(self) -> None:
        if self.feed is not None:
            return
        from shunkan.stream.bus import TickBus
        from shunkan.stream.factory import build_feed

        feed = await asyncio.to_thread(build_feed, load_watchlist())
        self.feed = feed
        self._base = list(feed.names.values())
        self.bus = TickBus(feed, self.loop)
        feed.ticker.start(feed.tokens, self._on_ticks, mode="quote")

    async def attach(self, ws: WebSocket):
        await self.ensure_feed()
        client = self.bus.add_client()
        # Every client starts with the watchlist — the boards and the tape
        # depend on it — and layers view subscriptions on top.
        subscribed, _ = self.bus.subscribe(client, self._base)
        # The hello rides the QUEUE, before the drain task exists. Sending it
        # straight on the socket made attach a second writer, and over a real
        # network a routed tick outran the hello. No await sits between
        # subscribe and push, so no dispatch callback can interleave: hello
        # is provably the first frame out.
        client.push({
            "type": "hello",
            "live": self.feed.live,
            "symbols": subscribed,
        })
        self._senders[ws] = (client, asyncio.create_task(self._drain(ws, client)))
        return client

    async def _drain(self, ws: WebSocket, client) -> None:
        """Sole writer to this websocket. A send that fails ends the task;
        the endpoint's finally block detaches the client."""
        try:
            while True:
                msg = await client.queue.get()
                await ws.send_text(json.dumps(msg))
                client.sent += 1
        except Exception:
            pass

    def handle_op(self, client, text: str) -> None:
        """Client protocol: {"op":"sub"|"unsub","symbols":[...]}.

        Acks ride the client's own queue (single-writer rule). Unknown
        symbols come back NAMED — a view that will stay dark should know."""
        try:
            msg = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            client.push({"type": "error", "message": "unparseable op"})
            return
        op = msg.get("op")
        symbols = [str(x) for x in (msg.get("symbols") or [])]
        if op == "sub" and symbols:
            _, unknown = self.bus.subscribe(client, symbols)
            client.push({"type": "subs", "subscribed": sorted(client.symbols),
                         "unknown": unknown})
        elif op == "unsub" and symbols:
            self.bus.unsubscribe(client, symbols)
            client.push({"type": "subs", "subscribed": sorted(client.symbols),
                         "unknown": []})
        elif op == "ping":
            client.push({"type": "pong"})
        else:
            client.push({"type": "error", "message": f"unknown op {op!r}"})

    def detach(self, ws: WebSocket) -> None:
        pair = self._senders.pop(ws, None)
        if pair is not None:
            client, task = pair
            task.cancel()
            if self.bus is not None:
                self.bus.remove_client(client)
        if not self._senders and not self.keepalive:
            self.stop()

    def stop(self) -> None:
        if self.feed is not None:
            self.feed.ticker.stop()
            self.feed = None
            self.bus = None

    def _on_ticks(self, ticks) -> None:
        """Ticker-thread callback: build bars, hand the frame to the bus."""
        feed, bus = self.feed, self.bus
        if feed is None or bus is None:
            return
        # Bars are built from every tick (only persisted for the live feed —
        # demo random-walk data must never enter the store).
        if feed.live:
            names = feed.names
            for t in ticks:
                self.bars.on_tick(names.get(t.token, str(t.token)), t.ltp, float(t.volume))
        bus.publish_threadsafe(ticks)

    async def broadcast(self, message: dict) -> None:
        if self.bus is not None:
            self.bus.broadcast(message)

    def stats(self) -> dict:
        if self.bus is None:
            return {"clients": 0, "symbols": [], "ticks": 0, "dropped": 0}
        return self.bus.stats()
