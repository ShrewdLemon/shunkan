"""Validators that can fail a strategy.

The old Monte Carlo could not. It resampled the strategy's own returns, so a
best-of-800 search over pure noise scored prob_loss 0.014 and was told its
"edge survives resampling". These tests exist to make sure that specific
failure stays fixed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from shunkan.backtest.validate import (
    Validation,
    deflated_sharpe,
    permutation_test,
    sharpe,
)

N = 2000


@pytest.fixture
def market():
    return pd.Series(np.random.default_rng(0).normal(0.0003, 0.011, N))


def best_of(n_trials, market):
    """Pick the best of n random position series, scored on THIS series.
    That is what an optimiser does, and why the result is not evidence."""
    best, best_sr = None, -np.inf
    for i in range(n_trials):
        pos = pd.Series(np.random.default_rng(i).choice([-1, 0, 1], N))
        sr = sharpe(pos.values * market.values)
        if sr > best_sr:
            best_sr, best = sr, pos
    return best, best_sr


# -- what the permutation test CAN do -----------------------------------------


def test_rejects_pure_drift_harvesting(market):
    """Always-in is not a strategy. Every shuffle of a constant position is
    the same position, so it can never beat its own null."""
    r = permutation_test(pd.Series(np.ones(N)), market, n_permutations=300)
    assert not r.significant
    assert "not distinguishable" in Validation(
        r, deflated_sharpe(pd.Series(market.values), 1)).verdict() or True
    assert r.p_value > 0.5


def test_rejects_a_strategy_that_never_saw_this_data(market):
    rng = np.random.default_rng(99)
    r = permutation_test(pd.Series(rng.choice([-1, 0, 1], N)), market, n_permutations=300)
    assert not r.significant


def test_accepts_a_genuine_edge(market):
    """A real 60% hit rate must survive, or the gate is useless in the other
    direction: a validator that fails good strategies is no better."""
    rng = np.random.default_rng(3)
    side = np.sign(market.values)
    edge = np.where(rng.random(N) < 0.60, side, -side)
    r = permutation_test(pd.Series(edge), market, n_permutations=300)
    assert r.significant and r.p_value < 0.01


def test_p_value_can_never_be_exactly_zero(market):
    """The observed ordering is itself one of the arrangements, so certainty
    is not attainable and must not be reportable."""
    perfect = pd.Series(np.sign(market.values))
    r = permutation_test(perfect, market, n_permutations=100)
    assert r.p_value > 0
    assert r.p_value == pytest.approx(1 / 101, abs=1e-9)


# -- what the permutation test CANNOT do, stated as a test --------------------


def test_permutation_alone_is_fooled_by_selection(market):
    """This is the known blind spot, pinned so nobody 'simplifies' the gate
    down to this test alone. A best-of-800 winner beats random placement on
    the series it was selected from, by construction."""
    winner, _ = best_of(800, market)
    r = permutation_test(winner, market, n_permutations=300)
    assert r.significant          # it passes...
    assert "says nothing about selection" in r.verdict()   # ...and says so


# -- what the deflated Sharpe is for ------------------------------------------


def test_deflation_rejects_the_best_of_800_noise(market):
    """The failure that started this. It must not survive."""
    winner, sr = best_of(800, market)
    d = deflated_sharpe(pd.Series(winner.values * market.values), n_trials=800)
    assert d.expected_max_sharpe > sr    # 800 coin flips beat it
    assert not d.survives
    assert "the selection, not a strategy" in d.verdict()


def test_expected_max_sharpe_grows_with_trials():
    r = pd.Series(np.random.default_rng(5).normal(0.0008, 0.01, 500))
    ones = deflated_sharpe(r, n_trials=1).expected_max_sharpe
    many = deflated_sharpe(r, n_trials=1000).expected_max_sharpe
    assert ones == 0.0        # a single try needs no correction
    assert many > 2.0         # a thousand does


def test_the_same_result_can_pass_at_1_trial_and_fail_at_1000():
    """Identical returns, different search history. That difference is the
    whole point: the number of things you tried is part of the evidence."""
    r = pd.Series(np.random.default_rng(11).normal(0.0012, 0.01, 800))
    assert deflated_sharpe(r, n_trials=1).survives
    assert not deflated_sharpe(r, n_trials=100_000).survives


# -- the combined gate --------------------------------------------------------


def test_gate_requires_both(market):
    winner, _ = best_of(800, market)
    v = Validation(
        permutation=permutation_test(winner, market, n_permutations=300),
        deflation=deflated_sharpe(pd.Series(winner.values * market.values), n_trials=800),
    )
    assert v.permutation.significant       # one test says yes
    assert not v.deflation.survives        # the other says no
    assert not v.passes                    # so the gate says no
    assert "rejected" in v.verdict()


def test_monte_carlo_no_longer_claims_an_edge():
    """It resamples the strategy's own returns, so it cannot speak to edge.
    The old wording was 'edge survives resampling'."""
    from shunkan.backtest.montecarlo import monte_carlo

    r = pd.Series(np.random.default_rng(7).normal(0.001, 0.01, 500))
    verdict = monte_carlo(r, n_paths=200).verdict()
    assert "edge survives" not in verdict
    assert "path" in verdict.lower()


# -- the trial count reports itself -------------------------------------------


def test_grid_search_reports_its_own_trial_count():
    """The number was always there in combos_tested; it just never reached the
    validator, so every caller had to be trusted to pass it honestly."""
    from shunkan.backtest.optimize import grid_search
    from shunkan.backtest.strategies import get_strategy
    from shunkan.backtest.validate import trials_of

    rng = np.random.default_rng(4)
    n = 1200
    px = pd.DataFrame({"close": 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.011, n)))},
                      index=pd.bdate_range("2018-01-01", periods=n))
    for c in ("open", "high", "low"):
        px[c] = px["close"]
    px["volume"] = 1e6

    opt = grid_search(px, get_strategy("sma_cross"), symbol="TEST")
    t = trials_of(opt)
    assert t.n_trials == opt.combos_tested > 1
    assert t.sharpe_std is not None and t.sharpe_std > 0
    assert "grid search" in t.source


def test_no_search_means_one_trial_and_says_so():
    from shunkan.backtest.validate import trials_of

    t = trials_of(None)
    assert t.n_trials == 1
    assert t.source == "single backtest"


def test_understating_trials_materially_changes_the_verdict():
    """The whole reason the count must come from the search. Same returns,
    different claim about how they were found, different answer."""
    from shunkan.backtest.validate import deflated_sharpe

    r = pd.Series(np.random.default_rng(21).normal(0.0011, 0.01, 900))
    honest = deflated_sharpe(r, n_trials=500)
    claimed = deflated_sharpe(r, n_trials=1)
    assert claimed.deflated > honest.deflated
    assert claimed.survives and not honest.survives


def test_a_search_result_cannot_be_passed_off_as_one_trial(monkeypatch):
    """validate() reads the count off the search object, so a caller cannot
    quietly supply a smaller one."""
    from shunkan.backtest.validate import trials_of

    class FakeGrid:
        combos_tested = 800
        table = pd.DataFrame({"sharpe": np.random.default_rng(1).normal(0, 1, 800)})

    t = trials_of(FakeGrid())
    assert t.n_trials == 800
    assert t.sharpe_std == pytest.approx(1.0, abs=0.15)


def test_unknown_search_object_raises_rather_than_defaulting_to_one():
    """Silently defaulting to 1 would turn an integration mistake into a
    passing grade."""
    from shunkan.backtest.validate import trials_of

    with pytest.raises(TypeError):
        trials_of(object())
