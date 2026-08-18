"""The tick bus: routing, refcounts, and honest backpressure.

These run against a fake feed so every assertion is deterministic — the
websocket integration tests in test_server.py cover the wiring."""

import asyncio

import pytest

from shunkan.stream.bus import QUEUE_FRAMES, BusClient, TickBus
from shunkan.stream.factory import Feed
from shunkan.stream.ticker import KiteTicker


class FakeTicker:
    def __init__(self):
        self.subs, self.unsubs = [], []

    def subscribe(self, tokens):
        self.subs.append(sorted(tokens))

    def unsubscribe(self, tokens):
        self.unsubs.append(sorted(tokens))


class FakeTick:
    def __init__(self, token, ltp):
        self.token, self.ltp = token, ltp
        self.change_pct, self.volume, self.oi = 0.1, 10, 0
        self.high = self.low = ltp


KNOWN = {"NIFTY": (1, "NIFTY"), "RELIANCE": (2, "RELIANCE")}


def make_bus(loop):
    tk = FakeTicker()
    feed = Feed(ticker=tk, tokens=[1], names={1: "NIFTY"}, live=False,
                resolve=lambda s: KNOWN.get(s))
    return TickBus(feed, loop), tk


def run(coro):
    return asyncio.run(coro)


def test_routing_is_per_client():
    async def main():
        bus, _ = make_bus(asyncio.get_running_loop())
        a, b = bus.add_client(), bus.add_client()
        bus.subscribe(a, ["NIFTY", "RELIANCE"])
        bus.subscribe(b, ["RELIANCE"])
        bus.dispatch([FakeTick(1, 100.0), FakeTick(2, 200.0)])
        assert len(a.queue.get_nowait()["data"]) == 2
        assert [r["symbol"] for r in b.queue.get_nowait()["data"]] == ["RELIANCE"]
        # a client with no match gets NOTHING queued, not an empty frame
        assert b.queue.qsize() == 0

    run(main())


def test_unknown_symbols_are_named_not_swallowed():
    async def main():
        bus, tk = make_bus(asyncio.get_running_loop())
        c = bus.add_client()
        ok, unknown = bus.subscribe(c, ["NIFTY", "NOPE"])
        assert ok == ["NIFTY"] and unknown == ["NOPE"]
        assert tk.subs == [[1]]     # the miss never reached the feed

    run(main())


def test_refcounts_drive_the_feed_exactly_once():
    async def main():
        bus, tk = make_bus(asyncio.get_running_loop())
        a, b = bus.add_client(), bus.add_client()
        bus.subscribe(a, ["RELIANCE"])
        bus.subscribe(b, ["RELIANCE"])
        assert tk.subs == [[2]]                 # second taker: no re-subscribe
        bus.unsubscribe(a, ["RELIANCE"])
        assert tk.unsubs == []                  # still one taker
        bus.remove_client(b)
        assert tk.unsubs == [[2]]               # last one out turns it off

    run(main())


def test_backpressure_drops_oldest_and_counts():
    async def main():
        c = BusClient()
        for i in range(QUEUE_FRAMES + 5):
            c.push({"i": i})
        assert c.dropped == 5
        assert c.queue.get_nowait()["i"] == 5   # oldest went first

    run(main())


def test_kite_ticker_bookkeeping_without_network():
    """Desired-set updates must not require a socket: the reconnect path
    subscribes the full set, so bookkeeping alone is correct when down."""
    k = KiteTicker("key", "token")
    k.subscribe([10, 11])
    k.subscribe([11, 12])
    assert k._tokens == {10, 11, 12}
    k.unsubscribe([11, 99])       # 99 never subscribed: harmless
    assert k._tokens == {10, 12}


def test_front_future_rows_picks_nearest_unexpired():
    import pandas as pd

    from datetime import date
    from shunkan.stream.factory import front_future_rows

    nfo = pd.DataFrame({
        "name": ["NIFTY", "NIFTY", "NIFTY", "BANKNIFTY", "FINNIFTY"],
        "instrument_type": ["FUT", "FUT", "CE", "FUT", "FUT"],
        "expiry": ["2026-08-25", "2026-09-29", "2026-08-25", "2026-08-25", "2026-08-25"],
        "instrument_token": [111, 222, 333, 444, 555],
    })
    rows = front_future_rows(nfo, ("NIFTY", "BANKNIFTY"), date(2026, 8, 18))
    assert rows == [(111, "NIFTYFUT"), (444, "BANKNIFTYFUT")]
    # after the August future expires, September takes over - no dead token
    rows = front_future_rows(nfo, ("NIFTY",), date(2026, 8, 26))
    assert rows == [(222, "NIFTYFUT")]


def test_ensure_front_futures_repairs_a_running_feed(monkeypatch):
    import pandas as pd

    from shunkan.stream import factory as F

    tk = FakeTicker()
    feed = F.Feed(ticker=tk, tokens=[1], names={1: "NIFTY"}, live=True)
    nfo = pd.DataFrame({
        "name": ["NIFTY", "BANKNIFTY"], "instrument_type": ["FUT", "FUT"],
        "expiry": ["2099-01-27", "2099-01-27"],
        "instrument_token": [111, 222],
    })
    monkeypatch.setattr("shunkan.data.brokers.get_broker", lambda: object())
    monkeypatch.setattr("shunkan.data.kite_fno.load_instruments",
                        lambda broker, ex: nfo)
    labels = F.ensure_front_futures(feed)
    assert sorted(labels) == ["BANKNIFTYFUT", "NIFTYFUT"]
    assert tk.subs == [[111, 222]]              # subscribed onto the RUNNING ticker
    assert feed.names[111] == "NIFTYFUT"
    # second call: nothing to do, nothing re-subscribed
    assert sorted(F.ensure_front_futures(feed)) == ["BANKNIFTYFUT", "NIFTYFUT"]
    assert tk.subs == [[111, 222]]
    # a demo feed is left alone
    demo = F.Feed(ticker=FakeTicker(), tokens=[], names={}, live=False)
    assert F.ensure_front_futures(demo) == []


def test_symbols_already_in_the_feed_resolve_by_identity():
    async def main():
        tk = FakeTicker()
        # NIFTYFUT streams (in names) but the resolver knows nothing of it
        feed = Feed(ticker=tk, tokens=[1, 9], names={1: "NIFTY", 9: "NIFTYFUT"},
                    live=True, resolve=lambda s: KNOWN.get(s))
        bus = TickBus(feed, asyncio.get_running_loop())
        c = bus.add_client()
        ok, unknown = bus.subscribe(c, ["NIFTYFUT"])
        assert ok == ["NIFTYFUT"] and unknown == []
        bus.dispatch([FakeTick(9, 24250.0)])
        assert [r["symbol"] for r in c.queue.get_nowait()["data"]] == ["NIFTYFUT"]

    run(main())
