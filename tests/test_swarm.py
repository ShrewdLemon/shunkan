"""Particle-swarm optimizer: correctness, determinism, honesty, speed."""

import time

import numpy as np
import pytest

from shunkan.backtest import get_strategy, swarm_optimize
from shunkan.backtest.swarm import _pick_params


@pytest.fixture(scope="module")
def result(request):
    from shunkan.data.provider import SyntheticProvider

    prices = SyntheticProvider().history("TEST", period="2y")
    return swarm_optimize(prices, get_strategy("sma_cross"), symbol="TEST",
                          n_particles=12, n_iters=10, landscape_res=11)


def test_history_shapes(result):
    assert len(result.iterations) == 10
    for it in result.iterations:
        assert it.positions.shape == (12, 2)
        assert it.fitness.shape == (12,)
        assert it.gbest.shape == (2,)


def test_gbest_monotone_nondecreasing(result):
    fits = [it.gbest_fitness for it in result.iterations]
    assert all(b >= a for a, b in zip(fits, fits[1:]))
    # landscape cells may legitimately beat the swarm's own gbest
    assert result.best_fitness >= fits[-1]


def test_best_is_best_of_everything_evaluated(result):
    """gbest must dominate every particle fitness AND every landscape cell."""
    all_fits = np.concatenate([it.fitness for it in result.iterations])
    assert result.best_fitness >= all_fits.max() - 1e-12
    assert result.best_fitness >= np.nanmax(result.landscape_z) - 1e-12


def test_landscape_real_and_shaped(result):
    assert result.landscape_z.shape == (11, 11)
    assert np.isfinite(result.landscape_z).all()
    # a real fitness landscape is not flat
    assert result.landscape_z.std() > 0


def test_best_params_within_bounds_and_integer(result):
    (bx, by) = result.bounds
    px, py = result.param_names
    assert px == "fast" and py == "slow"
    assert bx[0] <= result.best_params[px] <= bx[1]
    assert by[0] <= result.best_params[py] <= by[1]
    assert isinstance(result.best_params[px], int)
    assert isinstance(result.best_params[py], int)


def test_degenerate_combos_penalized_not_crowned(result):
    """fast >= slow must never win."""
    assert result.best_params["fast"] < result.best_params["slow"]


def test_deterministic_given_seed():
    from shunkan.data.provider import SyntheticProvider

    prices = SyntheticProvider().history("TEST", period="1y")
    strat = get_strategy("sma_cross")
    a = swarm_optimize(prices, strat, n_particles=8, n_iters=5, landscape_res=7)
    b = swarm_optimize(prices, strat, n_particles=8, n_iters=5, landscape_res=7)
    assert a.best_params == b.best_params
    assert a.best_fitness == b.best_fitness
    np.testing.assert_array_equal(a.iterations[-1].positions,
                                  b.iterations[-1].positions)


def test_memoization_caps_backtest_count(result):
    # 12x10 particle evals + 121 landscape cells, heavily overlapping ints
    assert result.n_evals <= 12 * 10 + 121
    assert result.n_evals >= 11 * 11  # at least the landscape grid


def test_single_param_strategy_rejected():
    with pytest.raises(ValueError, match="needs >= 2"):
        _pick_params(get_strategy("momentum"))


def test_metrics_come_from_real_backtest(result):
    m = result.best_metrics
    assert "sharpe" in m and "max_drawdown" in m
    assert abs(m["sharpe"] - result.best_fitness) < 1e-9


def test_swarm_speed_budget():
    """Full swarm + landscape on 2y daily inside 3 s (typically ~0.5 s)."""
    from shunkan.data.provider import SyntheticProvider

    prices = SyntheticProvider().history("TEST", period="2y")
    t0 = time.perf_counter()
    swarm_optimize(prices, get_strategy("sma_cross"),
                   n_particles=16, n_iters=12, landscape_res=15)
    assert time.perf_counter() - t0 < 3.0
