"""The tick bus: routes live ticks to the consumers that asked for them.

Before this, /ws/ticks was a firehose. The feed subscribed the watchlist once
at first attach, every client received every tick of every symbol, and a
stalled client made the broadcast path queue frames without bound. Three
different problems, one shared cause: nothing owned the question "who wants
what right now".

The bus owns it:

ROUTING. Each client holds a set of symbols; a tick reaches the clients whose
set contains it and nobody else. Subscriptions change per view on the
frontend, so opening a chart on a symbol outside the watchlist starts its
ticks and leaving the view stops them.

REFCOUNTS DRIVE THE FEED. The first client to want a symbol triggers a real
subscribe on the exchange socket (Kite allows this mid-session); the last to
leave triggers the unsubscribe. The feed streams exactly the union of what
somebody is looking at.

BOUNDED QUEUES, COUNTED DROPS. Each client drains its own queue; when a slow
consumer lets it fill, the OLDEST frame is dropped — a tape is latest-wins —
and the drop is counted where /api/status can see it. Losing data silently is
the one thing this codebase never does; losing it visibly under backpressure
is a design decision, stated here.

Threading: tick callbacks arrive on the ticker thread. dispatch() is handed
to the event loop via call_soon_threadsafe and does only put_nowait work, so
there is no unbounded task pileup and no lock shared with the hot path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

QUEUE_FRAMES = 200   # ~1 minute of full-rate frames; beyond that the client is gone


@dataclass(eq=False)  # identity semantics: clients live in a set
class BusClient:
    """One consumer: a websocket, or anything else that drains a queue."""

    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_FRAMES))
    symbols: set[str] = field(default_factory=set)
    dropped: int = 0
    sent: int = 0

    def push(self, message: dict) -> None:
        """Enqueue without blocking; under pressure drop the oldest frame."""
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait(message)
            except asyncio.QueueFull:
                self.dropped += 1


class TickBus:
    """Symbol-level pub/sub between one tick feed and many clients."""

    def __init__(self, feed, loop: asyncio.AbstractEventLoop) -> None:
        self.feed = feed
        self.loop = loop
        self.clients: set[BusClient] = set()
        self._refs: dict[str, int] = {}          # symbol -> how many clients want it
        self._token_of: dict[str, int] = {}      # resolved symbol -> feed token
        self.tick_count = 0

    # -- membership --------------------------------------------------------

    def add_client(self) -> BusClient:
        c = BusClient()
        self.clients.add(c)
        return c

    def remove_client(self, client: BusClient) -> None:
        self.clients.discard(client)
        if client.symbols:
            self.unsubscribe(client, sorted(client.symbols))

    # -- subscriptions -----------------------------------------------------

    def subscribe(self, client: BusClient, symbols: list[str]) -> tuple[list[str], list[str]]:
        """Returns (subscribed, unknown). Unknown symbols are NAMED, not
        silently ignored — the client finds out its view will stay dark."""
        ok: list[str] = []
        unknown: list[str] = []
        fresh_tokens: list[int] = []
        for raw in symbols:
            sym = str(raw).upper().removesuffix(".NS")
            if sym in client.symbols:
                ok.append(sym)
                continue
            token = self._resolve(sym)
            if token is None:
                unknown.append(sym)
                continue
            client.symbols.add(sym)
            ok.append(sym)
            self._refs[sym] = self._refs.get(sym, 0) + 1
            if self._refs[sym] == 1:
                fresh_tokens.append(token)
        if fresh_tokens:
            self.feed.ticker.subscribe(fresh_tokens)
        return ok, unknown

    def unsubscribe(self, client: BusClient, symbols: list[str]) -> list[str]:
        gone_tokens: list[int] = []
        removed: list[str] = []
        for raw in symbols:
            sym = str(raw).upper().removesuffix(".NS")
            if sym not in client.symbols:
                continue
            client.symbols.discard(sym)
            removed.append(sym)
            n = self._refs.get(sym, 0) - 1
            if n <= 0:
                self._refs.pop(sym, None)
                token = self._token_of.get(sym)
                if token is not None:
                    gone_tokens.append(token)
            else:
                self._refs[sym] = n
        if gone_tokens:
            self.feed.ticker.unsubscribe(gone_tokens)
        return removed

    def _resolve(self, sym: str) -> int | None:
        if sym in self._token_of:
            return self._token_of[sym]
        hit = self.feed.resolve(sym)
        if hit is None:
            return None
        token, name = hit
        self._token_of[sym] = token
        self.feed.names[token] = name    # so tick frames carry the symbol
        return token

    # -- the hot path ------------------------------------------------------

    def publish_threadsafe(self, ticks) -> None:
        """Called on the ticker thread. One callback per frame, no futures."""
        self.loop.call_soon_threadsafe(self.dispatch, ticks)

    def dispatch(self, ticks) -> None:
        """On the event loop: route this frame to interested clients."""
        if not self.clients:
            return
        names = self.feed.names
        rows = [
            {"symbol": names.get(t.token, str(t.token)), "ltp": t.ltp,
             "change_pct": round(t.change_pct, 6), "volume": t.volume,
             "oi": t.oi, "high": t.high, "low": t.low}
            for t in ticks
        ]
        self.tick_count += len(rows)
        for c in self.clients:
            mine = [r for r in rows if r["symbol"] in c.symbols]
            if mine:
                c.push({"type": "ticks", "live": self.feed.live, "data": mine})

    def broadcast(self, message: dict) -> None:
        """Non-tick messages (alerts) go to every client, no symbol filter."""
        for c in self.clients:
            c.push(message)

    def stats(self) -> dict:
        return {
            "clients": len(self.clients),
            "symbols": sorted(self._refs),
            "ticks": self.tick_count,
            "dropped": sum(c.dropped for c in self.clients),
        }
