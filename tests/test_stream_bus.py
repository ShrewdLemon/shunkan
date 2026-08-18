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
