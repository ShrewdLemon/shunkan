import numpy as np
import pandas as pd
import pytest

from shunkan.analytics import indicators as ta


@pytest.fixture
def series():
    rng = np.random.default_rng(7)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 300))))


def test_sma_matches_manual(series):
    out = ta.sma(series, 10)
    assert np.isnan(out.iloc[8])
    expected = series.iloc[0:10].mean()
    assert out.iloc[9] == pytest.approx(expected)
    assert out.iloc[-1] == pytest.approx(series.iloc[-10:].mean())


def test_ema_warmup_and_tracking(series):
    out = ta.ema(series, 12)
    assert out.isna().sum() == 11
    # EMA stays within the range of the data
    assert out.dropna().between(series.min(), series.max()).all()


def test_rsi_bounds(series):
    out = ta.rsi(series, 14).dropna()
    assert len(out) > 0
    assert (out >= 0).all() and (out <= 100).all()


def test_rsi_all_gains_pegged_at_100():
    up = pd.Series(np.arange(1.0, 51.0))
    out = ta.rsi(up, 14).dropna()
    assert (out == 100.0).all()


def test_macd_columns(series):
    out = ta.macd(series)
    assert list(out.columns) == ["macd", "signal", "histogram"]
    tail = out.dropna()
    assert np.allclose(tail["histogram"], tail["macd"] - tail["signal"])


def test_bollinger_band_ordering(series):
    bands = ta.bollinger(series, 20, 2.0).dropna()
    assert (bands["upper"] >= bands["middle"]).all()
    assert (bands["middle"] >= bands["lower"]).all()


def test_atr_positive(prices):
    out = ta.atr(prices, 14).dropna()
    assert (out > 0).all()


def test_vwap_within_range(prices):
    out = ta.vwap(prices).dropna()
    assert (out > 0).all()


def test_drawdown_non_positive(series):
    dd = ta.drawdown(series)
    assert (dd <= 1e-12).all()
    assert dd.iloc[0] == 0.0


def test_stochastic_bounds(prices):
    out = ta.stochastic(prices).dropna()
    assert ((out["k"] >= 0) & (out["k"] <= 100)).all()


def test_indicators_accept_uppercase_columns(prices):
    upper = prices.rename(columns=str.capitalize)
    out = ta.atr(upper, 14).dropna()
    assert len(out) > 0


def test_adx_bounds_and_warmup(prices):
    out = ta.adx(prices, 14)
    assert out.iloc[:14].isna().all()  # needs warm-up
    valid = out.dropna()
    assert len(valid) > 0
    assert (valid >= 0).all() and (valid <= 100).all()


def test_adx_strong_trend_high():
    # A clean monotonic uptrend should read as a strong trend.
    close = np.arange(1.0, 81.0)
    df = pd.DataFrame(
        {"open": close, "high": close + 0.5, "low": close - 0.5,
         "close": close, "volume": np.full(len(close), 1_000)},
        index=pd.bdate_range("2020-01-01", periods=len(close)),
    )
    assert ta.adx(df, 14).dropna().iloc[-1] > 40.0


def test_cci_zero_centered(prices):
    out = ta.cci(prices, 20).dropna()
    assert len(out) > 0
    # CCI swings both sides of zero on real-ish data.
    assert (out > 0).any() and (out < 0).any()


def test_williams_r_bounds(prices):
    out = ta.williams_r(prices, 14).dropna()
    assert ((out >= -100.0) & (out <= 0.0)).all()


def test_obv_moves_with_direction():
    df = pd.DataFrame(
        {"open": [10, 10, 10, 10], "high": [10, 11, 11, 10],
         "low": [10, 10, 10, 9], "close": [10, 11, 12, 11],
         "volume": [100, 200, 300, 400]},
        index=pd.bdate_range("2020-01-01", periods=4),
    )
    out = ta.obv(df)
    # up, up, down -> +200, +300, -400 cumulatively from a 0 baseline.
    assert out.tolist() == [0.0, 200.0, 500.0, 100.0]
