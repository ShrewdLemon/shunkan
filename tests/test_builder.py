import numpy as np
import pandas as pd
import pytest

from shunkan.backtest.builder import (
    INDICATORS,
    OPERATORS,
    Condition,
    RuleSpec,
    combine,
    compile_spec,
    eval_condition,
    resolve,
)


def _frame(closes, highs=None, lows=None, opens=None, vols=None):
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    return pd.DataFrame(
        {
            "open": opens if opens is not None else closes,
            "high": highs if highs is not None else closes * 1.01,
            "low": lows if lows is not None else closes * 0.99,
            "close": closes,
            "volume": vols if vols is not None else np.full(len(closes), 1_000_000.0),
        },
        index=idx,
    )


def test_resolve_price_refs(prices):
    assert resolve(prices, _op("CLOSE")).equals(prices["close"].astype(float))
    assert len(resolve(prices, _op("RSI", 14)).dropna()) > 0


def _op(indicator, period=14):
    from shunkan.backtest.builder import Operand

    return Operand(indicator=indicator, period=period)


def test_eval_condition_constant():
    df = _frame([10, 20, 30, 40])
    cond = Condition(left=_op("CLOSE"), op=">", value=25.0)
    out = eval_condition(df, cond)
    assert out.tolist() == [False, False, True, True]


def test_eval_condition_nan_warmup_is_false(prices):
    cond = Condition(left=_op("RSI", 14), op="<", value=50.0)
    out = eval_condition(prices, cond)
    # Warm-up bars (RSI NaN) must resolve to False, never NaN.
    assert out.dtype == bool
    assert not out.iloc[:14].any()


def test_cross_above():
    # fast SMA crossing a constant from below
    df = _frame([10, 10, 10, 20, 20])
    cond = Condition(left=_op("CLOSE"), op="cross_above", value=15.0)
    out = eval_condition(df, cond)
    assert out.tolist() == [False, False, False, True, False]


def test_cross_indicator_vs_indicator():
    # Dip then recover so the close genuinely crosses back above its SMA.
    df = _frame([10, 8, 6, 7, 9, 11, 13])
    cond = Condition(left=_op("CLOSE"), op="cross_above", right=_op("SMA", 3))
    out = eval_condition(df, cond)
    assert out.sum() >= 1
    assert out.dtype == bool


def test_combine_and_or():
    df = _frame([10, 20, 30, 40])
    c_gt = Condition(left=_op("CLOSE"), op=">", value=25.0)
    c_lt = Condition(left=_op("CLOSE"), op="<", value=15.0, join="OR")
    out = combine(df, [c_gt, c_lt])
    assert out.tolist() == [True, False, True, True]  # <15 OR >25
    c_and = Condition(left=_op("CLOSE"), op="<", value=35.0, join="AND")
    out2 = combine(df, [c_gt, c_and])
    assert out2.tolist() == [False, False, True, False]  # >25 AND <35


def test_rulespec_from_dict_and_validate():
    spec = RuleSpec.from_dict({
        "direction": "long",
        "long_entry": [{"left": {"indicator": "RSI", "period": 14}, "op": "<", "value": 30}],
        "long_exit": [{"left": {"indicator": "RSI", "period": 14}, "op": ">", "value": 70}],
    })
    assert spec.direction == "long"
    assert len(spec.long_entry) == 1
    assert "RSI(14)" in spec.long_entry[0].describe()


def test_rulespec_requires_entry():
    with pytest.raises(ValueError):
        RuleSpec.from_dict({"direction": "long", "long_entry": []})
    with pytest.raises(ValueError):
        RuleSpec.from_dict({"direction": "both",
                            "long_entry": [{"left": {"indicator": "RSI"}, "op": "<", "value": 30}]})


def test_invalid_indicator_and_operator():
    with pytest.raises(ValueError):
        Condition.from_dict({"left": {"indicator": "BOGUS"}, "op": "<", "value": 1})
    with pytest.raises(ValueError):
        Condition.from_dict({"left": {"indicator": "RSI"}, "op": "??", "value": 1})


def test_period_out_of_range():
    with pytest.raises(ValueError):
        Condition.from_dict({"left": {"indicator": "RSI", "period": 999}, "op": "<", "value": 1})


def test_compile_spec_sides(prices):
    spec = RuleSpec.from_dict({
        "direction": "long",
        "long_entry": [{"left": {"indicator": "RSI", "period": 14}, "op": "<", "value": 30}],
        "long_exit": [{"left": {"indicator": "RSI", "period": 14}, "op": ">", "value": 70}],
    })
    sig = compile_spec(prices, spec)
    assert sig.long_entry.dtype == bool
    assert not sig.short_entry.any()  # short side off for long-only
    assert len(sig.long_entry) == len(prices)


def test_catalog_consistency():
    # Every catalog indicator must resolve without raising.
    df = _frame(100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 120)))
    for kind, meta in INDICATORS.items():
        resolve(df, _op(kind, meta["default"] or 14))
    assert set(OPERATORS) >= {"<", ">", "cross_above", "cross_below"}
