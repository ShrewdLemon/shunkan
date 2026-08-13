import numpy as np
import pandas as pd
import pytest

from shunkan.analytics.volume import (
    analyze_volume,
    classify_day,
    detect_obv_divergence,
    obv,
    volume_profile,
)


def _frame(closes, volumes, spread=0.01):
    closes = np.asarray(closes, dtype=float)
    volumes = np.asarray(volumes, dtype=float)
    idx = pd.bdate_range("2026-01-01", periods=len(closes))
    return pd.DataFrame(
        {
            "open": closes * (1 - spread / 2),
            "high": closes * (1 + spread),
            "low": closes * (1 - spread),
            "close": closes,
            "volume": volumes,
        },
        index=idx,
    )


def test_volume_profile_poc_at_congestion():
    # Price spends most bars (and volume) around 100, briefly spikes to 120.
    closes = [100, 101, 99, 100, 100.5, 99.5, 120, 100, 100, 101]
    vols = [1e6] * 10
    prof = volume_profile(_frame(closes, vols), bins=12)
    assert 97 <= prof.poc <= 104  # POC sits in the congestion zone
    assert prof.value_area_low < prof.poc < prof.value_area_high


def test_obv_rises_with_up_moves():
    closes = np.array([100, 101, 102, 103.0])
    vols = np.array([1e6, 1e6, 1e6, 1e6])
    series = obv(closes, vols)
    assert series[-1] == pytest.approx(3e6)


def test_obv_divergence_detected():
    n = 60
    # Price grinds up; volume flows strongly on down bars (bearish divergence).
    closes = 100 + np.linspace(0, 5, n) + np.sin(np.arange(n)) * 2
    vols = np.where(np.diff(closes, prepend=closes[0]) < 0, 8e6, 1e6)
    result = detect_obv_divergence(closes, vols)
    assert "bearish" in result


def test_classify_day_accumulation():
    closes = [100] * 25
    # Trailing volume needs variance, otherwise z-score is degenerate-zero.
    rng = np.random.default_rng(3)
    vols = list(rng.normal(1e6, 1e5, 24)) + [4e6]
    frame = _frame(closes, vols)
    # Force last close near the high.
    frame.iloc[-1, frame.columns.get_loc("close")] = frame["high"].iloc[-1] * 0.999
    label, z, ratio = classify_day(frame)
    assert "accumulation" in label
    assert ratio > 1.5
    assert z > 1.0


def test_classify_day_quiet():
    closes = [100] * 25
    vols = [1e6] * 24 + [3e5]
    label, _, ratio = classify_day(_frame(closes, vols))
    assert "quiet" in label
    assert ratio < 0.6


def test_analyze_volume_full_report(prices):
    report = analyze_volume(prices)
    assert report.profile is not None
    assert report.day_type
    assert isinstance(report.notes, list)
    assert report.profile.value_area_high >= report.profile.value_area_low
