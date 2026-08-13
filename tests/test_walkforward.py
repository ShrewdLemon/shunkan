import numpy as np
import pandas as pd
import pytest

from shunkan.backtest import get_strategy, walk_forward
from shunkan.data.provider import SyntheticProvider


@pytest.fixture(scope="module")
def long_history():
    return SyntheticProvider().history("WFTEST", period="10y")


def test_walk_forward_structure(long_history):
    wf = walk_forward(
        long_history,
        get_strategy("sma_cross"),
        n_windows=4,
        param_grid={"fast": [10, 20], "slow": [50, 100]},
        symbol="WFTEST",
    )
    assert len(wf.windows) == 4
    assert wf.oos_equity is not None
    # Test segments tile contiguously and don't overlap their train windows.
    for w in wf.windows:
        assert w.train_end <= w.test_start
        assert w.best_params  # something was chosen every window
    assert wf.verdict in (
        "robust — parameters generalize out of sample",
        "fragile — some edge survives, size down expectations",
        "overfit — in-sample edge does not survive out of sample",
    )


def test_walk_forward_oos_equity_length(long_history):
    wf = walk_forward(
        long_history, get_strategy("sma_cross"), n_windows=4,
        param_grid={"fast": [10], "slow": [50]},
    )
    window_len = len(long_history) // 5
    assert len(wf.oos_equity) == 4 * window_len


def test_walk_forward_too_little_data():
    short = SyntheticProvider().history("TINY", period="3mo")
    with pytest.raises(ValueError, match="Not enough history"):
        walk_forward(short, get_strategy("sma_cross"), n_windows=8)


def test_buy_hold_efficiency_is_meaningful(long_history):
    """A parameterless-ish grid (single combo) can't overfit, so IS≈OOS
    behavior should keep efficiency away from pathological values."""
    wf = walk_forward(
        long_history, get_strategy("sma_cross"), n_windows=3,
        param_grid={"fast": [20], "slow": [50]},
    )
    assert wf.param_stability == pytest.approx(1.0)  # only one combo to pick
    assert np.isfinite(wf.efficiency)


def test_kite_instruments_csv_parsing():
    from shunkan.data.kite_fno import parse_instruments_csv

    csv_text = (
        "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,"
        "strike,tick_size,lot_size,instrument_type,segment,exchange\n"
        "12345,48,NIFTY26JUN23200CE,NIFTY,0,2026-06-18,23200,0.05,75,CE,NFO-OPT,NFO\n"
        "12346,48,NIFTY26JUN23200PE,NIFTY,0,2026-06-18,23200,0.05,75,PE,NFO-OPT,NFO\n"
        "256265,1001,NIFTY 50,NIFTY 50,0,,0,0.05,1,EQ,INDICES,NSE\n"
    )
    df = parse_instruments_csv(csv_text)
    assert len(df) == 3
    opts = df[df["instrument_type"].isin(["CE", "PE"])]
    assert (opts["strike"] == 23200.0).all()
    assert opts["expiry"].iloc[0].year == 2026


def test_kite_instruments_csv_missing_columns_raises():
    from shunkan.data.kite_fno import parse_instruments_csv
    from shunkan.data.provider import DataError

    with pytest.raises(DataError, match="missing columns"):
        parse_instruments_csv("a,b,c\n1,2,3\n")
