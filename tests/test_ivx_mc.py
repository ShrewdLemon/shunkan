import numpy as np
import pandas as pd
import pytest

from shunkan.backtest import get_strategy, monte_carlo, run_backtest
from shunkan.data.provider import SyntheticProvider
from shunkan.derivatives import analyze_vol, synthetic_chain
from shunkan.derivatives.ivx import realized_vol_cc, realized_vol_parkinson


@pytest.fixture(scope="module")
def hist():
    return SyntheticProvider().history("IVTEST", period="2y")


# -- realized vol -------------------------------------------------------------


def test_rv_cc_recovers_known_vol():
    rng = np.random.default_rng(11)
    true_vol = 0.18
    n = 2000
    rets = rng.normal(0, true_vol / np.sqrt(252), n)
    close = pd.Series(100 * np.exp(np.cumsum(rets)))
    rv = realized_vol_cc(close, window=252).dropna()
    assert rv.mean() == pytest.approx(true_vol, rel=0.1)


def test_rv_parkinson_positive(hist):
    rv = realized_vol_parkinson(hist["high"], hist["low"]).dropna()
    assert (rv > 0).all()
    assert rv.iloc[-1] < 2.0  # sane annualized bound


# -- vol report ----------------------------------------------------------------


def test_analyze_vol_report(hist):
    chain = synthetic_chain("IVTEST", spot=float(hist["close"].iloc[-1]))
    r = analyze_vol(chain, hist)
    assert 0 < r.atm_iv < 1
    assert 0 < r.rv_cc_21 < 2
    assert 0 <= r.rv_percentile <= 1
    assert set(r.cone) == {1, 3, 5, 10, 21}
    for days, (lo2, lo1, hi1, hi2) in r.cone.items():
        assert lo2 < lo1 < r.spot < hi1 < hi2
    # Cone widens with horizon.
    assert r.cone[21][3] - r.cone[21][0] > r.cone[1][3] - r.cone[1][0]


# -- monte carlo ----------------------------------------------------------------


def test_monte_carlo_structure(hist):
    bt = run_backtest(hist, get_strategy("buy_hold").signal(hist))
    mc = monte_carlo(bt.returns, n_paths=500, seed=3)
    assert mc.terminal_p5 <= mc.terminal_p50 <= mc.terminal_p95
    assert 0.0 <= mc.prob_loss <= 1.0
    assert mc.max_dd_p95 <= mc.max_dd_median <= 0.0
    assert len(mc.envelope_p50) == mc.n_bars
    assert (mc.envelope_p5 <= mc.envelope_p95 + 1e-12).all()


def test_monte_carlo_deterministic_seed(hist):
    bt = run_backtest(hist, get_strategy("buy_hold").signal(hist))
    a = monte_carlo(bt.returns, n_paths=200, seed=42)
    b = monte_carlo(bt.returns, n_paths=200, seed=42)
    assert a.terminal_p50 == b.terminal_p50
    assert a.prob_loss == b.prob_loss


def test_monte_carlo_positive_drift_low_loss_prob():
    rng = np.random.default_rng(1)
    rets = pd.Series(rng.normal(0.002, 0.005, 1000))  # strong steady edge
    mc = monte_carlo(rets, n_paths=500)
    assert mc.prob_loss < 0.05

    # The verdict describes the PATH, not the edge. It used to say "edge
    # survives resampling", which was false by construction: the bootstrap
    # resamples the strategy's own returns, so it inherits whatever mean they
    # had. See backtest.validate for the tests that can actually reject.
    verdict = mc.verdict()
    assert "favourable" in verdict
    assert "edge survives" not in verdict
    assert "backtest.validate" in verdict


def test_monte_carlo_too_short_raises():
    with pytest.raises(ValueError, match="at least"):
        monte_carlo(pd.Series([0.01] * 10))


def test_monte_carlo_speed(hist):
    bt = run_backtest(hist, get_strategy("buy_hold").signal(hist))
    mc = monte_carlo(bt.returns, n_paths=2000)
    assert mc.elapsed_ms < 500.0, f"MC took {mc.elapsed_ms:.0f}ms"
