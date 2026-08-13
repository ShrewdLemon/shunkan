"""World sessions and correlation matrix."""

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from shunkan.analytics.viz import correlation_matrix
from shunkan.markets import world_sessions

UTC = ZoneInfo("UTC")


def at(y, mo, d, h, mi, tz):
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def by_code(rows, code):
    return next(r for r in rows if r["code"] == code)


def test_nse_open_wednesday_midday():
    rows = world_sessions(at(2026, 7, 29, 11, 0, "Asia/Kolkata"))  # Wed
    nse = by_code(rows, "NSE")
    assert nse["open"] is True and nse["state"] == "open"


def test_nse_closed_sunday_everywhere():
    rows = world_sessions(at(2026, 7, 26, 11, 0, "Asia/Kolkata"))  # Sun
    assert all(not r["open"] for r in rows)


def test_tokyo_lunch_break_is_not_open():
    rows = world_sessions(at(2026, 7, 29, 12, 0, "Asia/Tokyo"))  # Wed 12:00 JST
    tse = by_code(rows, "TSE")
    assert tse["open"] is False and tse["state"] == "lunch"


def test_nyse_open_at_ten_local():
    rows = world_sessions(at(2026, 7, 29, 10, 0, "America/New_York"))
    assert by_code(rows, "NYSE")["open"] is True


def test_local_time_reported_in_exchange_tz():
    rows = world_sessions(at(2026, 7, 29, 10, 0, "Asia/Kolkata"))
    assert by_code(rows, "NSE")["local_time"] == "10:00"


# ---------------------------------------------------------------------------


def _closes(seed=3, n=200, cols=("A", "B", "C")):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-01", periods=n)
    base = rng.normal(0, 0.01, n)
    out = {}
    for i, c in enumerate(cols):
        noise = rng.normal(0, 0.01, n)
        # A and B share the base factor; C is pure noise
        r = base + noise * 0.3 if c in ("A", "B") else noise
        out[c] = pd.Series(100 * np.cumprod(1 + r), index=idx)
    return out


def test_correlation_shape_and_bounds():
    r = correlation_matrix(_closes())
    assert r.matrix.shape == (3, 3)
    np.testing.assert_allclose(np.diag(r.matrix), 1.0)
    np.testing.assert_allclose(r.matrix, r.matrix.T)
    assert (np.abs(r.matrix) <= 1.0 + 1e-12).all()


def test_correlated_pair_detected():
    r = correlation_matrix(_closes())
    a, b, c = r.top_pairs[0]
    assert {a, b} == {"A", "B"} and c > 0.8


def test_thin_symbols_dropped_not_faked():
    closes = _closes()
    closes["THIN"] = closes["A"].iloc[:10]
    r = correlation_matrix(closes)
    assert "THIN" in r.dropped
    assert "THIN" not in r.symbols


def test_all_thin_raises():
    closes = {k: v.iloc[:10] for k, v in _closes().items()}
    with pytest.raises(ValueError, match="overlapping daily returns"):
        correlation_matrix(closes)
