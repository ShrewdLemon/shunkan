"""Kite Connect WebSocket binary tick parser.

Zerodha streams market ticks as packed big-endian binary frames:

    frame   := n_packets:int16 (packet_len:int16 packet:bytes)*n
    packet  := ltp(8) | index_quote(28) | index_full(32) | quote(44) | full(184)

All int32 fields; prices are in paise (divide by 100) for NSE equity/F&O.
Parsing is allocation-light `struct.unpack_from` over memoryviews — a frame
with hundreds of instrument packets parses in tens of microseconds, which
is what lets the tape keep up with market-hours tick bursts.

Reference: kite.trade/docs/connect/v3/websocket
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

_H = struct.Struct(">H")
_I = struct.Struct(">i")

PRICE_DIVISOR = 100.0  # NSE equity/derivatives quote prices are in paise


@dataclass
class Tick:
    token: int
    ltp: float
    mode: str  # "ltp" | "quote" | "full" | "index"
    volume: int = 0
    oi: int = 0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0  # previous close

    @property
    def change_pct(self) -> float:
        if self.close > 0:
            return self.ltp / self.close - 1.0
        return 0.0


def parse_binary(data: bytes) -> list[Tick]:
    """Parse one websocket binary frame into ticks. Unknown sizes are skipped."""
    if len(data) < 2:
        return []  # heartbeat (1 byte) or empty
    view = memoryview(data)
    (n_packets,) = _H.unpack_from(view, 0)
    ticks: list[Tick] = []
    offset = 2
    for _ in range(n_packets):
        if offset + 2 > len(data):
            break
        (length,) = _H.unpack_from(view, offset)
        offset += 2
        if length < 8 or offset + length > len(data):
            break  # torn/truncated frame — drop the remainder, never raise
        packet = view[offset : offset + length]
        offset += length
        tick = _parse_packet(packet, length)
        if tick is not None:
            ticks.append(tick)
    return ticks


def _parse_packet(p: memoryview, length: int) -> Tick | None:
    (token,) = _I.unpack_from(p, 0)
    d = PRICE_DIVISOR

    if length == 8:  # LTP mode
        (ltp,) = _I.unpack_from(p, 4)
        return Tick(token=token, ltp=ltp / d, mode="ltp")

    if length in (28, 32):  # index quote / index full
        ltp, high, low, open_, close = (
            _I.unpack_from(p, 4)[0], _I.unpack_from(p, 8)[0],
            _I.unpack_from(p, 12)[0], _I.unpack_from(p, 16)[0],
            _I.unpack_from(p, 20)[0],
        )
        return Tick(
            token=token, ltp=ltp / d, mode="index",
            high=high / d, low=low / d, open=open_ / d, close=close / d,
        )

    if length in (44, 184):  # quote / full (full = quote + ts + OI + depth)
        ltp = _I.unpack_from(p, 4)[0]
        volume = _I.unpack_from(p, 16)[0]
        open_ = _I.unpack_from(p, 28)[0]
        high = _I.unpack_from(p, 32)[0]
        low = _I.unpack_from(p, 36)[0]
        close = _I.unpack_from(p, 40)[0]
        oi = _I.unpack_from(p, 48)[0] if length == 184 else 0
        return Tick(
            token=token, ltp=ltp / d, mode="full" if length == 184 else "quote",
            volume=volume, oi=oi,
            open=open_ / d, high=high / d, low=low / d, close=close / d,
        )

    return None  # unknown packet size — protocol drift; skip rather than crash


def build_frame(packets: list[bytes]) -> bytes:
    """Inverse of parse_binary — used by tests and the synthetic ticker."""
    out = _H.pack(len(packets))
    for p in packets:
        out += _H.pack(len(p)) + p
    return out


def build_ltp_packet(token: int, ltp: float) -> bytes:
    return _I.pack(token) + _I.pack(int(round(ltp * PRICE_DIVISOR)))


def build_quote_packet(
    token: int, ltp: float, volume: int = 0,
    open_: float = 0, high: float = 0, low: float = 0, close: float = 0,
) -> bytes:
    f = lambda v: _I.pack(int(round(v * PRICE_DIVISOR)))  # noqa: E731
    return (
        _I.pack(token) + f(ltp)
        + _I.pack(0) + f(0)              # last_qty, avg_price
        + _I.pack(volume)
        + _I.pack(0) + _I.pack(0)        # buy_qty, sell_qty
        + f(open_) + f(high) + f(low) + f(close)
    )
