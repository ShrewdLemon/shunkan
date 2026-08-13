"""Heston, Kalman, attention analogs."""

import numpy as np
import pytest

from shunkan.analytics.models import attention_analogs, heston_fan, kalman_trend


@pytest.fixture(scope="module")
def ohlc():
    from shunkan.data.provider import SyntheticProvider

    return SyntheticProvider().history("TEST", period="2y")


def test_heston_shapes_and_anchor():
    h = heston_fan(100.0, atm_iv=0.2, horizon=60, n_paths=500, n_display=20)
    assert h.display_paths.shape == (20, 60)
    assert h.v0 == pytest.approx(0.04)
    assert (h.envelope["p5"] <= h.envelope["p95"]).all()
    assert (h.display_paths > 0).all()


def test_heston_feller_flag():
    ok = heston_fan(100.0, 0.2, kappa=3.0, xi=0.3, n_paths=200, horizon=20)
    bad = heston_fan(100.0, 0.2, kappa=0.5, xi=2.0, n_paths=200, horizon=20)
    assert ok.feller_ok and not bad.feller_ok


def test_heston_vol_of_vol_deepens_left_tail():
    """With rho<0, higher xi buys crash risk: the p5 path ends lower."""
    calm = heston_fan(100.0, 0.2, kappa=3.0, xi=0.05, n_paths=1500, horizon=120)
    wild = heston_fan(100.0, 0.2, kappa=3.0, xi=0.45, n_paths=1500, horizon=120)
    assert calm.feller_ok and wild.feller_ok
    assert wild.envelope["p5"][-1] < calm.envelope["p5"][-1]


def test_heston_deterministic():
    a = heston_fan(100.0, 0.2, n_paths=300, horizon=30)
    b = heston_fan(100.0, 0.2, n_paths=300, horizon=30)
    np.testing.assert_array_equal(a.display_paths, b.display_paths)


def test_kalman_tracks_price(ohlc):
    close = ohlc[[c for c in ohlc.columns if c.lower() == "close"][0]].to_numpy()
    k = kalman_trend(close)
    assert len(k.level) == len(close)
    # filtered level should hug the price after burn-in
    rel = np.abs(k.level[50:] - close[50:]) / close[50:]
    assert np.median(rel) < 0.05
    assert np.isfinite(k.innovation_z).all()


def test_kalman_smoother_with_lower_q(ohlc):
    close = ohlc[[c for c in ohlc.columns if c.lower() == "close"][0]].to_numpy()
    smooth = kalman_trend(close, q=1e-7)
    twitchy = kalman_trend(close, q=1e-3)
    assert np.std(np.diff(smooth.slope)) < np.std(np.diff(twitchy.slope))


def test_kalman_refuses_thin():
    with pytest.raises(ValueError, match="30\\+"):
        kalman_trend(np.linspace(100, 110, 10))


def test_attention_rows_are_distributions(ohlc):
    a = attention_analogs(ohlc, window=60)
    assert a.matrix.shape == (60, 60)
    np.testing.assert_allclose(a.matrix.sum(axis=1), 1.0, rtol=1e-9)
    assert len(a.dates) == 60


def test_attention_top_analogs_exclude_today(ohlc):
    a = attention_analogs(ohlc, window=60, top_n=4)
    assert len(a.top_analogs) == 4
    today = a.dates[-1]
    assert all(t["date"] != today for t in a.top_analogs)
    assert all(0.0 <= t["weight"] <= 1.0 for t in a.top_analogs)


def test_attention_refuses_thin(ohlc):
    with pytest.raises(ValueError, match="usable days"):
        attention_analogs(ohlc.head(60), window=90)
