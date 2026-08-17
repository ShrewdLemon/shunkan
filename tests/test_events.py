"""Event studies: the three honesty rules, each pinned by a test that fails
without it."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from shunkan.analytics.events import (
    classify_today,
    event_study,
    excess_event_study,
    shock_days,
    standardised_returns,
)


def gbm(n=3000, mu=0.0003, vol=0.011, seed=0):
    r = np.random.default_rng(seed).normal(mu, vol, n)
    return pd.Series(100 * np.exp(np.cumsum(r)),
                     index=pd.bdate_range("2010-01-01", periods=n))


def test_no_lookahead_a_shock_is_judged_by_prior_vol():
    """One huge day after a long calm must classify as a shock using the vol
    that existed BEFORE it. If the day feeds its own sigma, the biggest events
    reclassify themselves as ordinary."""
    calm = np.full(300, 0.001)
    calm[250] = -0.08                       # a -8% day out of nowhere
    close = pd.Series(100 * np.exp(np.cumsum(calm)),
                      index=pd.bdate_range("2015-01-01", periods=300))
    z = standardised_returns(close)
    assert z.iloc[250] < -20                # enormous against PRIOR vol
    assert len(shock_days(close, sigma=2.0, direction="down")) >= 1


def test_clustered_events_count_once():
    """Two shocks a day apart share their forward window. Counting both turns
    one observation into two, which is how n gets inflated."""
    r = np.full(400, 0.0005)
    r[200] = -0.05
    r[201] = -0.05                          # same episode, next day
    close = pd.Series(100 * np.exp(np.cumsum(r)),
                      index=pd.bdate_range("2015-01-01", periods=400))
    res = event_study(close, "T", sigma=2.0, direction="down")
    assert res.n_events == 1
    assert "dropped" in res.note


def test_a_planted_effect_is_detected_against_the_baseline():
    rng = np.random.default_rng(7)
    r = rng.normal(0.0, 0.01, 4000)
    # Plant -6% days (z ~ -6) followed by 5 days of +40bps drift, and study at
    # sigma=4 so ordinary 2-sigma noise days cannot dilute the event set. The
    # first version of this test studied at sigma=2 and ~90 noise events
    # swamped ~25 planted ones, which is itself a lesson about event studies.
    for i in range(200, 3900, 150):
        r[i] = -0.06
        r[i + 1:i + 6] += 0.004
    close = pd.Series(100 * np.exp(np.cumsum(r)),
                      index=pd.bdate_range("2008-01-01", periods=4000))
    res = event_study(close, "T", sigma=4.0, direction="down")
    h5, b5 = res.horizons[5], res.baseline[5]
    assert res.n_events > 15
    assert h5.mean_pct > b5.mean_pct + 1.0   # well above unconditional
    assert h5.t_stat is not None and h5.t_stat > 3


def test_pure_noise_shows_no_conditional_edge():
    res = event_study(gbm(seed=3), "T", sigma=2.0, direction="down")
    h5 = res.horizons[5]
    if h5.n >= 8 and h5.t_stat is not None:
        assert abs(h5.t_stat) < 2.5          # no manufactured drift


def test_baseline_carries_no_t_stat():
    """Baseline windows overlap; a t-stat there would be fiction."""
    res = event_study(gbm(), "T")
    assert all(b.t_stat is None for b in res.baseline.values())


def test_excess_study_strips_the_market_move():
    """A stock that falls WITH the market and recovers WITH it has no excess
    reaction, whatever its raw path looks like."""
    rng = np.random.default_rng(11)
    mkt = rng.normal(0.0003, 0.012, 3000)
    mkt[1500] = -0.04                        # market-wide crash day
    mkt[1501:1506] += 0.006                  # market-wide recovery
    idx = pd.bdate_range("2012-01-01", periods=3000)
    bench = pd.Series(100 * np.exp(np.cumsum(mkt)), index=idx)
    stock = pd.Series(50 * np.exp(np.cumsum(mkt)), index=idx)  # beta-1 clone
    res = excess_event_study(stock, bench, "CLONE", sigma=2.0, direction="down")
    for h in (1, 5, 10):
        if res.horizons[h].n:
            assert abs(res.horizons[h].mean_pct) < 0.2   # nothing left


def test_classify_today_names_the_latest_session():
    r = np.full(300, 0.001)
    r[-1] = -0.05
    close = pd.Series(100 * np.exp(np.cumsum(r)),
                      index=pd.bdate_range("2020-01-01", periods=300))
    c = classify_today(close)
    assert c["classification"] == "down_shock"
    assert c["z"] < -2
