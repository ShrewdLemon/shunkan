"""Walk-forward validation: the honesty check for optimized parameters.

Grid-searching a full history overfits — the "best" parameters memorize the
past. Walk-forward splits history into rolling windows, optimizes on each
in-sample (train) segment, then evaluates those frozen parameters on the
following out-of-sample (test) segment. The stitched out-of-sample equity
curve is the realistic estimate of how the strategy would have traded.

A strategy is only as good as its OOS efficiency: out-of-sample Sharpe
relative to in-sample Sharpe. Below ~0.5, the optimizer was mostly fitting
noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from shunkan.analytics import stats
from shunkan.backtest.engine import BacktestConfig, run_backtest
from shunkan.backtest.optimize import grid_search
from shunkan.backtest.strategies import Strategy


@dataclass
class WalkForwardWindow:
    train_start: object
    train_end: object
    test_start: object
    test_end: object
    best_params: dict[str, Any]
    is_sharpe: float   # in-sample (train) sharpe of chosen params
    oos_sharpe: float  # out-of-sample (test) sharpe of those params
    oos_return: float


@dataclass
class WalkForwardResult:
    symbol: str
    strategy_name: str
    metric: str
    windows: list[WalkForwardWindow] = field(default_factory=list)
    oos_equity: pd.Series | None = None  # stitched out-of-sample curve
    oos_sharpe: float = 0.0
    oos_return: float = 0.0
    oos_max_dd: float = 0.0
    is_sharpe_mean: float = 0.0
    efficiency: float = 0.0  # oos_sharpe / mean in-sample sharpe
    param_stability: float = 0.0  # 1.0 = same params chosen every window

    @property
    def verdict(self) -> str:
        if not self.windows:
            return "insufficient data"
        if self.efficiency >= 0.7 and self.oos_sharpe > 0.5:
            return "robust — parameters generalize out of sample"
        if self.efficiency >= 0.4 and self.oos_sharpe > 0:
            return "fragile — some edge survives, size down expectations"
        return "overfit — in-sample edge does not survive out of sample"


def walk_forward(
    prices: pd.DataFrame,
    strategy: Strategy,
    metric: str = "sharpe",
    n_windows: int = 4,
    train_frac: float = 0.7,
    config: BacktestConfig | None = None,
    param_grid: dict[str, list] | None = None,
    symbol: str = "?",
) -> WalkForwardResult:
    """Anchored-rolling walk-forward: each window's train segment is followed
    by its test segment; test segments tile the back half of history."""
    cfg = config or BacktestConfig()
    grid = param_grid or strategy.param_grid
    if not grid:
        raise ValueError(f"Strategy '{strategy.name}' has no parameter grid")

    n = len(prices)
    window_len = n // (n_windows + 1)
    if window_len < 40:
        raise ValueError(
            f"Not enough history: {n} bars across {n_windows} windows "
            f"gives {window_len}-bar segments (need ≥40). Use a longer period."
        )
    train_len = int(window_len * train_frac / (1 - train_frac))

    result = WalkForwardResult(
        symbol=symbol, strategy_name=strategy.name, metric=metric
    )
    oos_curves: list[pd.Series] = []
    chosen_params: list[tuple] = []

    for w in range(n_windows):
        test_start_i = n - (n_windows - w) * window_len
        test_end_i = test_start_i + window_len
        train_start_i = max(test_start_i - train_len, 0)
        if test_start_i - train_start_i < 40:
            continue

        train = prices.iloc[train_start_i:test_start_i]
        test = prices.iloc[test_start_i:test_end_i]

        opt = grid_search(
            train, strategy, metric=metric, config=cfg,
            param_grid=grid, symbol=symbol,
        )
        params = opt.best_params
        if not params:
            continue
        chosen_params.append(tuple(sorted(params.items())))

        is_bt = run_backtest(
            train, strategy.signal(train, **params), cfg,
            symbol=symbol, strategy_name=strategy.name, params=params,
        )
        # Warm the indicators on the TRAINING data, then evaluate only on the
        # test window. Computing the signal on `test` alone left a 200-period
        # SMA undefined for the first 200 bars of every window, which is how a
        # 250-bar window became 50 tradeable bars and four windows became seven
        # out-of-sample trades in total.
        #
        # This adds no look-ahead: the warmup is strictly before test_start,
        # which is exactly the history a live trader would have had on that
        # morning. What it removes is a bias AGAINST the strategy, and a
        # validator that fails good strategies for the wrong reason is as
        # useless as one that passes bad ones.
        warm = prices.iloc[train_start_i:test_end_i]
        warm_signal = strategy.signal(warm, **params)
        test_signal = warm_signal.loc[test.index]
        oos_bt = run_backtest(
            test, test_signal, cfg,
            symbol=symbol, strategy_name=strategy.name, params=params,
        )
        oos_curves.append(oos_bt.returns)

        result.windows.append(
            WalkForwardWindow(
                train_start=train.index[0], train_end=train.index[-1],
                test_start=test.index[0], test_end=test.index[-1],
                best_params=params,
                is_sharpe=stats.sharpe(is_bt.returns),
                oos_sharpe=stats.sharpe(oos_bt.returns),
                oos_return=oos_bt.total_return,
            )
        )

    if not result.windows:
        return result

    stitched = pd.concat(oos_curves)
    equity = cfg.initial_cash * (1.0 + stitched).cumprod()
    result.oos_equity = equity
    result.oos_sharpe = stats.sharpe(stitched)
    result.oos_return = float(equity.iloc[-1] / cfg.initial_cash - 1.0)
    result.oos_max_dd = stats.max_drawdown(equity)
    result.is_sharpe_mean = float(np.mean([w.is_sharpe for w in result.windows]))
    if abs(result.is_sharpe_mean) > 1e-9:
        result.efficiency = max(result.oos_sharpe, 0.0) / max(result.is_sharpe_mean, 1e-9)
    unique = len(set(chosen_params))
    result.param_stability = 1.0 - (unique - 1) / max(len(chosen_params), 1)
    return result
