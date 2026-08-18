"""Candlestick detectors: deterministic shapes, honest records."""

import numpy as np
import pandas as pd
import pytest

from shunkan.analytics.candles import analyze_candles, detect_all, pattern_record


def frame(rows):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


def test_doji_and_marubozu():
    df = frame([
        [100.0, 105.0, 95.0, 100.2],    # doji: body 0.2 of range 10
        [100.0, 110.1, 99.9, 110.0],    # bullish marubozu: body ~= range
        [110.0, 110.1, 99.9, 100.0],    # bearish marubozu
    ])
    det = detect_all(df)
    assert list(det["pattern"]) == ["doji", "bullish marubozu", "bearish marubozu"]


def test_hammer_is_not_a_doji():
    # body 30% of range, long lower shadow, no upper: a hammer and only that
    df = frame([[100.0, 100.05, 91.0, 97.0]])
    det = detect_all(df)
    assert list(det["pattern"]) == ["hammer"]
    assert det["direction"].iloc[0] == "bullish"


def test_engulfing_pair():
    df = frame([
        [100.0, 101.0, 96.0, 97.0],     # red
        [96.5, 102.5, 96.0, 101.5],     # green swallowing it: bullish engulfing
        [101.0, 103.0, 100.5, 102.5],   # green
        [103.0, 103.5, 99.5, 100.0],    # red swallowing it: bearish engulfing
    ])
    pats = set(detect_all(df)["pattern"])
    assert "bullish engulfing" in pats and "bearish engulfing" in pats


def test_three_white_soldiers():
    df = frame([
        [100.0, 103.2, 99.9, 103.0],
        [103.0, 106.2, 102.9, 106.0],
        [106.0, 109.2, 105.9, 109.0],
    ])
    assert "three white soldiers" in set(detect_all(df)["pattern"])


def test_most_days_print_nothing():
    # ordinary candles: modest bodies, both shadows present
    rng = np.random.default_rng(3)
    rows = []
    px = 100.0
    for _ in range(30):
        o = px
        c = o * (1 + rng.normal(0, 0.004))
        h = max(o, c) * 1.004
        l = min(o, c) * 0.996
        rows.append([o, h, l, c])
        px = c
    det = detect_all(frame(rows))
    assert len(det) < 10          # patterns are the exception, not the norm


def test_record_carries_baseline_and_n():
    rows = [[100.0, 105.0, 95.0, 100.2]] + [
        [100.0, 101.5, 99.0, 100.5], [100.5, 102.0, 99.5, 101.0],
        [101.0, 102.5, 100.0, 101.5], [101.5, 103.0, 100.5, 102.0],
        [102.0, 103.5, 101.0, 102.5], [102.5, 104.0, 101.5, 103.0],
    ]
    df = frame(rows)
    det = detect_all(df)
    rec = pattern_record(df, det, "doji")
    assert rec["n"] >= 1
    h1 = rec["horizons"]["1"]
    assert h1 is not None and "baseline_pct" in h1 and h1["n"] >= 1


def test_analyze_shape():
    df = frame([[100.0, 105.0, 95.0, 100.2]] * 4)
    out = analyze_candles(df)
    assert "recent" in out and "note" in out
    for r in out["recent"]:
        assert {"date", "pattern", "direction", "record"} <= set(r)
