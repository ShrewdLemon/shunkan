import threading
import time

import pytest

from shunkan.stream import SyntheticTicker, parse_binary
from shunkan.stream.parser import (
    build_frame,
    build_ltp_packet,
    build_quote_packet,
)


def test_parse_ltp_packet():
    frame = build_frame([build_ltp_packet(256265, 23161.60)])
    ticks = parse_binary(frame)
    assert len(ticks) == 1
    assert ticks[0].token == 256265
    assert ticks[0].ltp == pytest.approx(23161.60)
    assert ticks[0].mode == "ltp"


def test_parse_quote_packet_with_change():
    frame = build_frame([
        build_quote_packet(738561, 1263.0, volume=13_000_000,
                           open_=1255.0, high=1270.0, low=1250.0, close=1258.5)
    ])
    tick = parse_binary(frame)[0]
    assert tick.mode == "quote"
    assert tick.ltp == pytest.approx(1263.0)
    assert tick.volume == 13_000_000
    assert tick.close == pytest.approx(1258.5)
    assert tick.change_pct == pytest.approx(1263.0 / 1258.5 - 1.0)


def test_parse_multi_packet_frame():
    frame = build_frame([
        build_ltp_packet(1, 100.0),
        build_quote_packet(2, 200.0, close=190.0),
        build_ltp_packet(3, 300.0),
    ])
    ticks = parse_binary(frame)
    assert [t.token for t in ticks] == [1, 2, 3]


def test_heartbeat_and_garbage_are_safe():
    assert parse_binary(b"") == []
    assert parse_binary(b"\x00") == []  # 1-byte heartbeat
    # Truncated frame: claims 2 packets, contains half of one.
    assert isinstance(parse_binary(b"\x00\x02\x00\x08\x00\x00"), list)


def test_unknown_packet_size_skipped():
    weird = b"\x00\x00\x00\x07" + b"\x00" * 9  # 13 bytes — not a known mode
    frame = build_frame([weird])
    assert parse_binary(frame) == []


def test_synthetic_ticker_streams_real_frames():
    received: list = []
    done = threading.Event()

    def on_tick(ticks):
        received.extend(ticks)
        if len(received) >= 4:
            done.set()

    ticker = SyntheticTicker(["RELIANCE", "NIFTY"], seed=42)
    ticker.start(None, on_tick)
    assert done.wait(timeout=5.0), "no ticks within 5s"
    ticker.stop()
    assert all(t.ltp > 0 for t in received)
    assert {t.token for t in received} <= set(ticker.tokens)


def test_synthetic_ticker_stops_cleanly():
    ticker = SyntheticTicker(["X"])
    ticker.start(None, lambda ticks: None)
    time.sleep(0.5)
    ticker.stop()
    time.sleep(0.6)
    assert not ticker._thread.is_alive()
