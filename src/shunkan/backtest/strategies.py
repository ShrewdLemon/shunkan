"""Built-in strategy library.

A Strategy maps an OHLCV DataFrame to a target-position signal series
(-1 short, 0 flat, +1 long). All strategies are vectorized.

Register new strategies with @register — they become available in the TUI
(`bt SYMBOL <name> ...`), the CLI, and the optimizer automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from shunkan.analytics import indicators as ta


@dataclass
class Strategy:
    name: str
    description: str
    build_signal: Callable[..., pd.Series]
    defaults: dict[str, int | float] = field(default_factory=dict)
    # parameter name -> values to try in grid search
    param_grid: dict[str, list] = field(default_factory=dict)

    def signal(self, prices: pd.DataFrame, **params) -> pd.Series:
        merged = {**self.defaults, **params}
        return self.build_signal(prices, **merged)


STRATEGIES: dict[str, Strategy] = {}


def register(strategy: Strategy) -> Strategy:
    STRATEGIES[strategy.name] = strategy
    return strategy


def get_strategy(name: str) -> Strategy:
    key = name.lower().replace("-", "_")
    if key not in STRATEGIES:
        raise KeyError(
            f"Unknown strategy '{name}'. Available: {', '.join(sorted(STRATEGIES))}"
        )
    return STRATEGIES[key]


def _close(prices: pd.DataFrame) -> pd.Series:
    cols = {c.lower(): c for c in prices.columns}
    return prices[cols["close"]].astype(float)


def _sma_cross(prices: pd.DataFrame, fast: int = 20, slow: int = 50) -> pd.Series:
    close = _close(prices)
    fast_ma = ta.sma(close, fast)
    slow_ma = ta.sma(close, slow)
    sig = pd.Series(np.where(fast_ma > slow_ma, 1.0, -1.0), index=close.index)
    sig[fast_ma.isna() | slow_ma.isna()] = 0.0
    return sig


def _ema_cross(prices: pd.DataFrame, fast: int = 12, slow: int = 26) -> pd.Series:
    close = _close(prices)
    fast_ma = ta.ema(close, fast)
    slow_ma = ta.ema(close, slow)
    sig = pd.Series(np.where(fast_ma > slow_ma, 1.0, -1.0), index=close.index)
    sig[fast_ma.isna() | slow_ma.isna()] = 0.0
    return sig


def _rsi_reversion(
    prices: pd.DataFrame, window: int = 14, oversold: float = 30, overbought: float = 70
) -> pd.Series:
    """Long when RSI dips below oversold, exit when it recovers past 50;
    short when above overbought, cover below 50. State carried via ffill."""
    close = _close(prices)
    r = ta.rsi(close, window)
    raw = pd.Series(np.nan, index=close.index)
    raw[r < oversold] = 1.0
    raw[r > overbought] = -1.0
    # Exit longs when RSI crosses back above 50, shorts when back below 50.
    holding = raw.ffill()
    exit_long = (holding == 1.0) & (r > 50)
    exit_short = (holding == -1.0) & (r < 50)
    raw[exit_long | exit_short] = 0.0
    sig = raw.ffill().fillna(0.0)
    sig[r.isna()] = 0.0
    return sig


def _bollinger_breakout(
    prices: pd.DataFrame, window: int = 20, num_std: float = 2.0
) -> pd.Series:
    """Trend-following breakout: long above the upper band, short below the
    lower band, flat when price crosses back through the middle band."""
    close = _close(prices)
    bands = ta.bollinger(close, window, num_std)
    raw = pd.Series(np.nan, index=close.index)
    raw[close > bands["upper"]] = 1.0
    raw[close < bands["lower"]] = -1.0
    holding = raw.ffill()
    raw[(holding == 1.0) & (close < bands["middle"])] = 0.0
    raw[(holding == -1.0) & (close > bands["middle"])] = 0.0
    sig = raw.ffill().fillna(0.0)
    sig[bands["middle"].isna()] = 0.0
    return sig


def _momentum(prices: pd.DataFrame, window: int = 63) -> pd.Series:
    """Time-series momentum: long if trailing return positive, short if negative."""
    close = _close(prices)
    mom = ta.momentum(close, window)
    sig = pd.Series(np.sign(mom), index=close.index).fillna(0.0)
    return sig


def _macd_trend(
    prices: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.Series:
    close = _close(prices)
    m = ta.macd(close, fast, slow, signal)
    sig = pd.Series(np.where(m["macd"] > m["signal"], 1.0, -1.0), index=close.index)
    sig[m["macd"].isna()] = 0.0
    return sig


def _buy_hold(prices: pd.DataFrame) -> pd.Series:
    close = _close(prices)
    return pd.Series(1.0, index=close.index)


register(
    Strategy(
        name="sma_cross",
        description="SMA crossover: long when fast SMA > slow SMA, short otherwise",
        build_signal=_sma_cross,
        defaults={"fast": 20, "slow": 50},
        param_grid={"fast": [5, 10, 20, 30, 50], "slow": [50, 100, 150, 200]},
    )
)
register(
    Strategy(
        name="ema_cross",
        description="EMA crossover: long when fast EMA > slow EMA, short otherwise",
        build_signal=_ema_cross,
        defaults={"fast": 12, "slow": 26},
        param_grid={"fast": [5, 9, 12, 20], "slow": [26, 50, 100, 200]},
    )
)
register(
    Strategy(
        name="rsi_reversion",
        description="Mean reversion on RSI extremes; exits at the RSI midline",
        build_signal=_rsi_reversion,
        defaults={"window": 14, "oversold": 30, "overbought": 70},
        param_grid={"window": [7, 14, 21], "oversold": [20, 25, 30], "overbought": [70, 75, 80]},
    )
)
register(
    Strategy(
        name="bollinger_breakout",
        description="Breakout beyond Bollinger bands, exit at the middle band",
        build_signal=_bollinger_breakout,
        defaults={"window": 20, "num_std": 2.0},
        param_grid={"window": [10, 20, 30], "num_std": [1.5, 2.0, 2.5]},
    )
)
register(
    Strategy(
        name="momentum",
        description="Time-series momentum on trailing returns",
        build_signal=_momentum,
        defaults={"window": 63},
        param_grid={"window": [21, 42, 63, 126, 252]},
    )
)
register(
    Strategy(
        name="macd_trend",
        description="Long when MACD above its signal line, short below",
        build_signal=_macd_trend,
        defaults={"fast": 12, "slow": 26, "signal": 9},
        param_grid={"fast": [8, 12], "slow": [21, 26, 34], "signal": [5, 9]},
    )
)
register(
    Strategy(
        name="buy_hold",
        description="Buy and hold benchmark",
        build_signal=_buy_hold,
    )
)
