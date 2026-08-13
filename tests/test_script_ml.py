"""Shun Script DSL sandbox + evaluation, and the numpy ML studio."""

import numpy as np
import pandas as pd
import pytest

from shunkan.analytics import indicators as ta
from shunkan.ml import train_model
from shunkan.script import ScriptError, run_script


@pytest.fixture(scope="module")
def ohlc(provider_module):
    return provider_module.history("TEST", period="2y")


@pytest.fixture(scope="module")
def provider_module():
    from shunkan.data.provider import SyntheticProvider

    return SyntheticProvider()


# ------------------------------------------------------------- DSL: evaluation

def test_script_plots_match_direct_indicators(ohlc):
    r = run_script("fast = ema(close, 12)\nplot(fast, color='amber', title='F')", ohlc)
    assert len(r.plots) == 1
    p = r.plots[0]
    assert p["title"] == "F" and p["color"] == "#f0a826"
    close = ohlc[[c for c in ohlc.columns if c.lower() == "close"][0]].astype(float)
    pd.testing.assert_series_equal(p["values"], ta.ema(close, 12), check_names=False)


def test_script_signal_and_arithmetic(ohlc):
    src = """
fast = ema(close, 10)
slow = ema(close, 40)
spread = (fast - slow) / slow
long_when(cross_above(fast, slow))
short_when(cross_below(fast, slow))
exit_when(abs(spread) < 0.001)
"""
    r = run_script(src, ohlc)
    assert r.signal is not None
    assert set(np.unique(r.signal)) <= {-1.0, 0.0, 1.0}
    assert (r.signal != 0).any()
    assert "spread" in r.variables


def test_script_bool_logic_and_compare(ohlc):
    r = run_script("hot = rsi(close, 14) > 70\ncold = rsi(close, 14) < 30\n"
                   "long_when(cold)\nshort_when(hot and volume > 0)", ohlc)
    assert r.signal is not None


def test_script_error_carries_line_number(ohlc):
    with pytest.raises(ScriptError, match="line 2"):
        run_script("x = ema(close, 5)\ny = nope(x)", ohlc)


# ------------------------------------------------------------- DSL: sandbox

@pytest.mark.parametrize("evil", [
    "import os",
    "__import__('os')",
    "open('/etc/passwd')",
    "close.__class__",
    "(lambda: 1)()",
    "for i in close: pass",
    "x = [1,2,3]",
    "exec('1')",
    "close[0]",
    "while True: pass",
])
def test_script_sandbox_rejects(evil, ohlc):
    with pytest.raises(ScriptError):
        run_script(evil, ohlc)


def test_script_cannot_shadow_builtins(ohlc):
    with pytest.raises(ScriptError, match="reassign"):
        run_script("close = 1", ohlc)


# ------------------------------------------------------------- ML studio

FEATS = ["ret1", "ret5", "rsi14", "ema_gap", "vol20"]


@pytest.fixture(scope="module")
def trained(ohlc):
    return train_model(ohlc, FEATS, model="stumps", horizon=5)


def test_ml_split_is_chronological_and_sized(trained):
    assert trained.n_train > trained.n_test > 40
    assert len(trained.test_index) == len(trained.equity_model)


def test_ml_metrics_in_range(trained):
    for v in (trained.acc_train, trained.acc_test, trained.baseline_test):
        assert 0.0 <= v <= 1.0
    assert abs(sum(trained.importances.values()) - 1.0) < 1e-9


def test_ml_ridge_also_runs(ohlc):
    r = train_model(ohlc, FEATS, model="ridge", horizon=5)
    assert r.model == "ridge" and 0.0 <= r.acc_test <= 1.0


def test_ml_deterministic(ohlc):
    a = train_model(ohlc, FEATS, model="stumps")
    b = train_model(ohlc, FEATS, model="stumps")
    assert a.acc_test == b.acc_test
    np.testing.assert_array_equal(a.equity_model, b.equity_model)


def test_ml_refuses_thin_data(ohlc):
    with pytest.raises(ValueError, match="not enough to say anything honest"):
        train_model(ohlc.head(100), FEATS)


def test_ml_refuses_unknown_model(ohlc):
    with pytest.raises(ValueError, match="ridge' or 'stumps"):
        train_model(ohlc, FEATS, model="lstm")
