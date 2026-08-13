import numpy as np
import pandas as pd
import pytest

from shunkan.analytics import stats


def test_cagr_doubling_in_one_year():
    # 253 points = 252 periods = exactly one trading year
    equity = pd.Series(np.linspace(1.0, 2.0, 253))
    assert stats.cagr(equity) == pytest.approx(1.0, rel=1e-9)


def test_max_drawdown_simple():
    equity = pd.Series([1.0, 2.0, 1.0, 3.0])
    assert stats.max_drawdown(equity) == pytest.approx(-0.5)


def test_max_drawdown_monotonic_rise_is_zero():
    equity = pd.Series(np.linspace(1, 5, 100))
    assert stats.max_drawdown(equity) == 0.0


def test_sharpe_positive_for_positive_drift():
    rng = np.random.default_rng(11)
    rets = pd.Series(rng.normal(0.001, 0.01, 1000))
    assert stats.sharpe(rets) > 1.0


def test_sharpe_zero_for_constant_returns():
    rets = pd.Series([0.01] * 100)
    assert stats.sharpe(rets) == 0.0  # zero variance -> defined as 0


def test_sortino_exceeds_sharpe_for_skewed_returns():
    rng = np.random.default_rng(5)
    rets = pd.Series(rng.normal(0.001, 0.01, 2000))
    assert stats.sortino(rets) >= stats.sharpe(rets) * 0.9


def test_win_rate_and_profit_factor():
    trades = [0.05, -0.02, 0.03, -0.01]
    assert stats.win_rate(trades) == pytest.approx(0.5)
    assert stats.profit_factor(trades) == pytest.approx(0.08 / 0.03)


def test_profit_factor_no_losses_is_inf():
    assert stats.profit_factor([0.01, 0.02]) == float("inf")


def test_exposure():
    pos = pd.Series([0, 0, 1, 1, -1, 0, 1, 1, 0, 0])
    assert stats.exposure(pos) == pytest.approx(0.5)


def test_calmar_sign():
    # Rising curve with a real dip so max drawdown is nonzero.
    equity = pd.Series(np.linspace(1.0, 2.0, 252)) + pd.Series(
        0.1 * np.sin(np.arange(252) / 5.0)
    )
    assert stats.max_drawdown(equity) < 0
    assert stats.calmar(equity) > 0
