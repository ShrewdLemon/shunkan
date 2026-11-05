"""Kite Connect WebSocket binary tick parser.

Zerodha streams market ticks as packed big-endian binary frames:

    frame   := n_packets:int16 (packet_len:int16 packet:bytes)*n
    packet  := ltp(8) | index_quote(28) | index_full(32) | quote(44) | full(184)

All int32 fields; prices are in paise (divide by 100) for NSE equity/F&O.
Parsing is allocation-light `struct.unpack_from` over memoryviews — a frame
with hundreds of instrument packets parses in tens of microseconds, which
is what lets the tape keep up with market-hours tick bursts.

The 184-byte FULL packet carries five levels of bid/ask depth in its last
120 bytes. Those are parsed into `Tick.bids` / `Tick.asks`; every other mode
leaves them empty. Depth is what any execution model has to price against —
LTP alone cannot tell you what you would actually have been filled at.

Reference: kite.trade/docs/connect/v3/websocket
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

_H = struct.Struct(">H")
_I = struct.Struct(">i")
_DEPTH = struct.Struct(">iihh")  # quantity, price, orders, padding

PRICE_DIVISOR = 100.0  # NSE equity/derivatives quote prices are in paise

DEPTH_OFFSET = 64      # first depth entry in a FULL packet
DEPTH_LEVELS = 5       # per side


@dataclass(frozen=True)
class DepthLevel:
    """One price level of the order book."""

    price: float
    quantity: int
    orders: int


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
    # Present on quote/full packets only.
    last_qty: int = 0
    atp: float = 0.0            # average traded price for the day
    total_buy_qty: int = 0
    total_sell_qty: int = 0
    # Present on full packets only.
    exchange_ts: int = 0        # epoch seconds, exchange clock
    bids: list[DepthLevel] = field(default_factory=list)   # best first
    asks: list[DepthLevel] = field(default_factory=list)   # best first

    @property
    def change_pct(self) -> float:
        if self.close > 0:
            return self.ltp / self.close - 1.0
        return 0.0

    @property
    def bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def spread(self) -> float:
        """Absolute touch-to-touch spread, 0.0 when the book is unknown."""
        return self.ask - self.bid if (self.bids and self.asks) else 0.0

    @property
    def mid(self) -> float:
        """Midpoint, falling back to LTP when there is no book."""
        if self.bids and self.asks:
            return (self.bid + self.ask) / 2.0
        return self.ltp

    @property
    def microprice(self) -> float:
        """Size-weighted touch price — leans toward the side likely to trade.

        With a thin bid and a fat offer, fair value sits nearer the bid.
        Falls back to `mid` when either touch has no quantity.
        """
        if not (self.bids and self.asks):
            return self.ltp
        bq, aq = self.bids[0].quantity, self.asks[0].quantity
        if bq + aq <= 0:
            return self.mid
        return (self.bid * aq + self.ask * bq) / (bq + aq)


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


def _parse_depth(p: memoryview) -> tuple[list[DepthLevel], list[DepthLevel]]:
    """Ten 12-byte depth entries: five bids then five asks, best first.

    A zero-price level means the book is thinner than five deep; it is
    dropped rather than recorded as a real level at price 0.0, which would
    otherwise poison every spread and mid computed downstream.
    """
    bids: list[DepthLevel] = []
    asks: list[DepthLevel] = []
    for i in range(DEPTH_LEVELS * 2):
        qty, price, orders, _pad = _DEPTH.unpack_from(p, DEPTH_OFFSET + i * 12)
        if price <= 0:
            continue
        level = DepthLevel(price=price / PRICE_DIVISOR, quantity=qty, orders=orders)
        (bids if i < DEPTH_LEVELS else asks).append(level)
    return bids, asks


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
        last_qty = _I.unpack_from(p, 8)[0]
        atp = _I.unpack_from(p, 12)[0]
        volume = _I.unpack_from(p, 16)[0]
        tbq = _I.unpack_from(p, 20)[0]
        tsq = _I.unpack_from(p, 24)[0]
        open_ = _I.unpack_from(p, 28)[0]
        high = _I.unpack_from(p, 32)[0]
        low = _I.unpack_from(p, 36)[0]
        close = _I.unpack_from(p, 40)[0]
        oi = _I.unpack_from(p, 48)[0] if length == 184 else 0
        exch_ts = _I.unpack_from(p, 60)[0] if length == 184 else 0
        bids, asks = _parse_depth(p) if length == 184 else ([], [])
        return Tick(
            token=token, ltp=ltp / d, mode="full" if length == 184 else "quote",
            volume=volume, oi=oi,
            open=open_ / d, high=high / d, low=low / d, close=close / d,
            last_qty=last_qty, atp=atp / d,
            total_buy_qty=tbq, total_sell_qty=tsq,
            exchange_ts=exch_ts, bids=bids, asks=asks,
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


def build_full_packet(
    token: int, ltp: float, volume: int = 0, oi: int = 0,
    open_: float = 0, high: float = 0, low: float = 0, close: float = 0,
    bids: list[tuple[float, int, int]] | None = None,
    asks: list[tuple[float, int, int]] | None = None,
    last_qty: int = 0, atp: float = 0.0, exchange_ts: int = 0,
) -> bytes:
    """Build a 184-byte FULL packet. `bids`/`asks` are (price, qty, orders),
    best first; short sides are zero-padded the way the exchange pads them."""
    f = lambda v: _I.pack(int(round(v * PRICE_DIVISOR)))  # noqa: E731
    head = (
        _I.pack(token) + f(ltp)
        + _I.pack(last_qty) + f(atp)
        + _I.pack(volume)
        + _I.pack(0) + _I.pack(0)        # total buy/sell qty
        + f(open_) + f(high) + f(low) + f(close)
        + _I.pack(0)                     # last traded timestamp
        + _I.pack(oi)
        + _I.pack(0) + _I.pack(0)        # OI day high / low
        + _I.pack(exchange_ts)
    )
    body = b""
    for side in (bids or [], asks or []):
        for i in range(DEPTH_LEVELS):
            if i < len(side):
                price, qty, orders = side[i]
                body += _DEPTH.pack(qty, int(round(price * PRICE_DIVISOR)), orders, 0)
            else:
                body += _DEPTH.pack(0, 0, 0, 0)
    return head + body
