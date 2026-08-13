import numpy as np
import pandas as pd
import pytest

from shunkan.backtest.builder import CompiledSignals
from shunkan.backtest.simulate import ExecConfig, simulate

ZERO = dict(commission=0.0, slippage=0.0)


def _frame(o, h, low, c, index=None):
    o, h, low, c = (np.asarray(x, float) for x in (o, h, low, c))
    idx = index if index is not None else pd.bdate_range("2020-01-01", periods=len(c))
    return pd.DataFrame(
        {"open": o, "high": h, "low": low, "close": c, "volume": np.full(len(c), 1e6)},
        index=idx,
    )


def _sig(df, long_entry=None, long_exit=None, short_entry=None, short_exit=None):
    n = len(df)
    z = lambda v: pd.Series(v if v is not None else [False] * n, index=df.index)
    return CompiledSignals(z(long_entry), z(long_exit), z(short_entry), z(short_exit))


def test_entry_fills_next_bar_open():
    # Signal on bar0 -> position opens at bar1's OPEN, not bar0.
    df = _frame([10, 100, 110, 120], [10, 101, 111, 121],
                [10, 99, 109, 119], [10, 105, 115, 125])
    sig = _sig(df, long_entry=[True, False, False, False])
    res = simulate(df, sig, ExecConfig(**ZERO))
    assert len(res.trades) == 1
    assert res.trades[0].entry_price == pytest.approx(100.0)
    assert res.trades[0].entry_time == df.index[1].to_pydatetime()


def test_take_profit_intrabar():
    df = _frame([10, 100, 100, 100], [10, 106, 100, 100],
                [10, 99, 99, 99], [10, 104, 100, 100])
    sig = _sig(df, long_entry=[True, False, False, False])
    res = simulate(df, sig, ExecConfig(tp_mode="percent", tp_value=5.0, **ZERO))
    t = res.trades[0]
    assert t.exit_reason == "target"
    assert t.exit_price == pytest.approx(105.0)  # 5% above 100
    assert res.final_equity == pytest.approx(res.initial_cash * 1.05)


def test_stop_loss_intrabar():
    df = _frame([10, 100, 100, 100], [10, 101, 100, 100],
                [10, 94, 99, 99], [10, 96, 100, 100])
    sig = _sig(df, long_entry=[True, False, False, False])
    res = simulate(df, sig, ExecConfig(sl_mode="percent", sl_value=5.0, **ZERO))
    t = res.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(95.0)
    assert res.final_equity == pytest.approx(res.initial_cash * 0.95)


def test_gap_through_stop_fills_at_open():
    # Bar opens below the stop -> you eat the gap, fill at the open.
    df = _frame([10, 90, 90, 90], [10, 92, 92, 92], [10, 88, 88, 88], [10, 90, 90, 90])
    sig = _sig(df, long_entry=[True, False, False, False])
    res = simulate(df, sig, ExecConfig(sl_mode="percent", sl_value=5.0, **ZERO))
    # entry at open of bar1... but bar1 IS the entry bar (open 90). Entry price = 90,
    # stop = 85.5; bar1 low 88 > stop so no exit. Use a later gap instead:
    assert res.trades  # smoke; precise gap covered below


def test_gap_open_below_stop():
    df = _frame([10, 100, 80, 80], [10, 101, 82, 82], [10, 99, 78, 78], [10, 100, 80, 80])
    sig = _sig(df, long_entry=[True, False, False, False])
    res = simulate(df, sig, ExecConfig(sl_mode="percent", sl_value=5.0, **ZERO))
    t = res.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(80.0)  # filled at the gapped-down open, not 95


def test_signal_exit_telescopes():
    df = _frame([10, 100, 110, 120, 130], [10, 106, 116, 126, 136],
                [10, 99, 109, 119, 129], [10, 105, 115, 125, 135])
    sig = _sig(df, long_entry=[True, False, False, False, False],
               long_exit=[False, False, True, False, False])
    res = simulate(df, sig, ExecConfig(**ZERO))
    t = res.trades[0]
    assert t.exit_reason == "signal"
    assert t.exit_price == pytest.approx(120.0)  # open of bar3
    assert t.bars_held == 2
    assert res.final_equity == pytest.approx(res.initial_cash * 1.2)


def test_short_take_profit():
    df = _frame([10, 100, 100, 100], [10, 101, 100, 100],
                [10, 94, 99, 99], [10, 96, 100, 100])
    sig = _sig(df, short_entry=[True, False, False, False])
    res = simulate(df, sig, ExecConfig(tp_mode="percent", tp_value=5.0, **ZERO))
    t = res.trades[0]
    assert t.direction == -1
    assert t.exit_reason == "target"
    assert res.final_equity == pytest.approx(res.initial_cash * 1.05)


def test_allow_short_false_blocks_shorts():
    df = _frame([10, 100, 90, 90], [10, 101, 91, 91], [10, 99, 89, 89], [10, 100, 90, 90])
    sig = _sig(df, short_entry=[True, True, True, True])
    res = simulate(df, sig, ExecConfig(allow_short=False, **ZERO))
    assert res.trades == []


def test_trailing_stop_locks_profit():
    df = _frame([10, 100, 116, 111], [10, 100, 120, 112],
                [10, 100, 115, 108], [10, 100, 118, 110])
    sig = _sig(df, long_entry=[True, False, False, False])
    res = simulate(df, sig, ExecConfig(sl_mode="percent", sl_value=10.0, trailing=True, **ZERO))
    t = res.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(110.0)  # trailed up to 120-10
    assert t.return_pct == pytest.approx(0.10)
    assert res.final_equity == pytest.approx(res.initial_cash * 1.10)


def test_cooldown_throttles_reentry():
    o = [100] * 10
    df = _frame(o, [102] * 10, [99] * 10, [100] * 10)
    le = [True] * 10
    cfg_kw = dict(tp_mode="percent", tp_value=1.0, **ZERO)  # exits same bar every time
    none = simulate(df, _sig(df, long_entry=le), ExecConfig(cooldown_bars=0, **cfg_kw))
    cooled = simulate(df, _sig(df, long_entry=le), ExecConfig(cooldown_bars=2, **cfg_kw))
    assert len(none.trades) > len(cooled.trades)
    assert len(cooled.trades) == 3  # entries at bars 1, 4, 7


def test_session_filter_blocks_off_hours():
    idx = pd.date_range("2020-01-01 06:00", periods=8, freq="1h")
    df = _frame([100] * 8, [102] * 8, [99] * 8, [100] * 8, index=idx)
    res = simulate(
        df, _sig(df, long_entry=[True] * 8),
        ExecConfig(tp_mode="percent", tp_value=1.0,
                   session_start="09:00", session_end="13:00", **ZERO),
    )
    assert res.trades  # some entries happened
    for t in res.trades:
        assert 9 <= t.entry_time.hour <= 13


def test_perf_10y_daily_under_60ms():
    from shunkan.backtest.builder import RuleSpec, compile_spec
    from shunkan.data.provider import SyntheticProvider

    big = SyntheticProvider().history("SIMPERF", period="10y")
    spec = RuleSpec.from_dict({
        "direction": "long",
        "long_entry": [{"left": {"indicator": "EMA", "period": 12}, "op": "cross_above",
                        "right": {"indicator": "EMA", "period": 26}}],
        "long_exit": [{"left": {"indicator": "EMA", "period": 12}, "op": "cross_below",
                       "right": {"indicator": "EMA", "period": 26}}],
    })
    sig = compile_spec(big, spec)
    res = simulate(big, sig, ExecConfig(sl_mode="atr", sl_value=2.0, tp_mode="atr", tp_value=3.0))
    assert res.elapsed_ms < 60.0
    assert len(res.equity) == len(big)
