"""Tick stream clients: live Kite WebSocket and an offline synthetic ticker.

Both expose the same tiny interface so the UI doesn't care which is wired:

    ticker = KiteTicker(api_key, access_token) | SyntheticTicker(symbols)
    ticker.start(tokens, on_tick)   # on_tick(list[Tick]) from a worker thread
    ticker.stop()
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable

import numpy as np

from shunkan.stream.parser import (
    Tick,
    build_frame,
    build_quote_packet,
    parse_binary,
)

OnTick = Callable[[list[Tick]], None]


class KiteTicker:
    """Minimal Kite WebSocket client: subscribe in quote mode, parse binary
    frames, auto-reconnect with capped backoff. Runs its own event loop in
    a daemon thread so it drops into any sync app."""

    WS_URL = "wss://ws.kite.trade"

    def __init__(self, api_key: str, access_token: str) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, tokens: list[int], on_tick: OnTick, mode: str = "quote") -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, args=(tokens, on_tick, mode), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- internals ---------------------------------------------------------

    def _run_loop(self, tokens: list[int], on_tick: OnTick, mode: str) -> None:
        import asyncio

        asyncio.run(self._stream(tokens, on_tick, mode))

    async def _stream(self, tokens: list[int], on_tick: OnTick, mode: str) -> None:
        import asyncio

        import websockets

        url = f"{self.WS_URL}?api_key={self.api_key}&access_token={self.access_token}"
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, max_size=2**20) as ws:
                    backoff = 1.0
                    await ws.send(json.dumps({"a": "subscribe", "v": tokens}))
                    await ws.send(json.dumps({"a": "mode", "v": [mode, tokens]}))
                    while not self._stop.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        if isinstance(msg, bytes):
                            ticks = parse_binary(msg)
                            if ticks:
                                on_tick(ticks)
            except Exception:
                if self._stop.is_set():
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)


class SyntheticTicker:
    """Random-walk tick generator with the same interface, for offline mode.

    Emits realistic frames (built with the real binary encoder, decoded by
    the real parser) at ~3 frames/sec so the tape panel and tests exercise
    the exact same code path as live streaming.
    """

    def __init__(self, symbols: list[str], seed: int = 7) -> None:
        self.symbols = symbols
        # Stable fake tokens per symbol: index in list + 1000.
        self.tokens = {1000 + i: s for i, s in enumerate(symbols)}
        self._rng = np.random.default_rng(seed)
        self._prices = {
            t: 500.0 + float(self._rng.uniform(0, 20000)) for t in self.tokens
        }
        self._closes = {t: p * (1 + float(self._rng.normal(0, 0.01)))
                        for t, p in self._prices.items()}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, tokens: list[int] | None, on_tick: OnTick, mode: str = "quote") -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(on_tick,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, on_tick: OnTick) -> None:
        vol = {t: 0 for t in self.tokens}
        while not self._stop.is_set():
            packets = []
            for token in self.tokens:
                drift = float(self._rng.normal(0, self._prices[token] * 4e-4))
                self._prices[token] = max(self._prices[token] + drift, 1.0)
                vol[token] += int(self._rng.integers(100, 5000))
                packets.append(
                    build_quote_packet(
                        token,
                        self._prices[token],
                        volume=vol[token],
                        close=self._closes[token],
                    )
                )
            on_tick(parse_binary(build_frame(packets)))
            time.sleep(0.33)
