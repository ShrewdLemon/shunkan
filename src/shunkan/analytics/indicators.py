"""Technical indicators, all vectorized over numpy arrays / pandas Series.

Every function accepts a pandas Series (or DataFrame where noted) and
returns a Series/DataFrame aligned to the input index, with NaN for the
warm-up period.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder's smoothing)."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # When avg_loss == 0 (all gains), RSI is pegged at 100.
    out = out.where(avg_loss != 0.0, 100.0)
    out[avg_gain.isna() | avg_loss.isna()] = np.nan
    return out


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """MACD line, signal line, and histogram."""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame(
        {
            "macd": macd_line,
            "signal": signal_line,
            "histogram": macd_line - signal_line,
        }
    )


def bollinger(series: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """Bollinger bands: middle (SMA), upper, lower, and %B."""
    mid = sma(series, window)
    std = series.rolling(window=window, min_periods=window).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = upper - lower
    pct_b = (series - lower) / width.replace(0.0, np.nan)
    return pd.DataFrame({"middle": mid, "upper": upper, "lower": lower, "pct_b": pct_b})


def atr(ohlc: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average True Range. Expects columns: high, low, close (case-insensitive)."""
    df = _normalize_ohlc(ohlc)
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def vwap(ohlc: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price (cumulative). Expects high/low/close/volume."""
    df = _normalize_ohlc(ohlc)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vol = df["volume"].cumsum()
    return (typical * df["volume"]).cumsum() / cum_vol.replace(0.0, np.nan)


def returns(series: pd.Series) -> pd.Series:
    """Simple period-over-period returns."""
    return series.pct_change()


def log_returns(series: pd.Series) -> pd.Series:
    """Log returns."""
    return np.log(series / series.shift(1))


def drawdown(equity: pd.Series) -> pd.Series:
    """Drawdown series (<= 0) from running peak."""
    peak = equity.cummax()
    return equity / peak - 1.0


def momentum(series: pd.Series, window: int = 63) -> pd.Series:
    """Total return over the trailing window (e.g. 63 bars ~ 3 months)."""
    return series / series.shift(window) - 1.0


def stochastic(ohlc: pd.DataFrame, k_window: int = 14, d_window: int = 3) -> pd.DataFrame:
    """Stochastic oscillator %K / %D."""
    df = _normalize_ohlc(ohlc)
    low_min = df["low"].rolling(k_window, min_periods=k_window).min()
    high_max = df["high"].rolling(k_window, min_periods=k_window).max()
    rng = (high_max - low_min).replace(0.0, np.nan)
    k = 100.0 * (df["close"] - low_min) / rng
    d = k.rolling(d_window, min_periods=d_window).mean()
    return pd.DataFrame({"k": k, "d": d})


def _true_range(df: pd.DataFrame) -> pd.Series:
    """True range from a normalized high/low/close frame."""
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def adx(ohlc: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average Directional Index (Wilder). Trend *strength*, 0–100, direction-agnostic.

    Returns the ADX line only; values above ~25 conventionally read as a
    trending market, below ~20 as range-bound.
    """
    df = _normalize_ohlc(ohlc)
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0.0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0.0), down, 0.0), index=df.index)

    alpha = 1.0 / window
    atr_ = _true_range(df).ewm(alpha=alpha, adjust=False, min_periods=window).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=window).mean() / atr_
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=window).mean() / atr_
    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=alpha, adjust=False, min_periods=window).mean()


def cci(ohlc: pd.DataFrame, window: int = 20) -> pd.Series:
    """Commodity Channel Index. Oscillates around 0; ±100 marks the usual extremes."""
    df = _normalize_ohlc(ohlc)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    sma_tp = tp.rolling(window, min_periods=window).mean()
    # Mean absolute deviation about the window mean (not a rolling mean of |dev|).
    mad = tp.rolling(window, min_periods=window).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True
    )
    return (tp - sma_tp) / (0.015 * mad.replace(0.0, np.nan))


def williams_r(ohlc: pd.DataFrame, window: int = 14) -> pd.Series:
    """Williams %R. Ranges -100 (at period low) to 0 (at period high)."""
    df = _normalize_ohlc(ohlc)
    high_max = df["high"].rolling(window, min_periods=window).max()
    low_min = df["low"].rolling(window, min_periods=window).min()
    rng = (high_max - low_min).replace(0.0, np.nan)
    return -100.0 * (high_max - df["close"]) / rng


def obv(ohlc: pd.DataFrame) -> pd.Series:
    """On-Balance Volume: a running sum of volume signed by the close-to-close move."""
    df = _normalize_ohlc(ohlc)
    direction = np.sign(df["close"].diff().fillna(0.0))
    return (direction * df["volume"]).cumsum()


def _normalize_ohlc(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Return a view with lowercase column names so callers can pass yfinance frames."""
    return ohlc.rename(columns={c: c.lower() for c in ohlc.columns})
