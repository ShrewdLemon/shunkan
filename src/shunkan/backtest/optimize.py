"""Grid-search parameter optimization.

Because the engine is fully vectorized, sweeping hundreds of parameter
combinations on daily data takes well under a second.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd

from shunkan.backtest.engine import BacktestConfig, run_backtest
from shunkan.backtest.strategies import Strategy


@dataclass
class OptimizationResult:
    strategy_name: str
    metric: str
    table: pd.DataFrame  # one row per combo, sorted by metric desc
    best_params: dict[str, Any]
    combos_tested: int
    elapsed_s: float


def grid_search(
    prices: pd.DataFrame,
    strategy: Strategy,
    metric: str = "sharpe",
    config: BacktestConfig | None = None,
    param_grid: dict[str, list] | None = None,
    symbol: str = "?",
    max_combos: int = 2000,
) -> OptimizationResult:
    grid = param_grid or strategy.param_grid
    if not grid:
        raise ValueError(f"Strategy '{strategy.name}' has no parameter grid to search")

    names = list(grid)
    combos = list(itertools.product(*(grid[n] for n in names)))
    if len(combos) > max_combos:
        raise ValueError(f"{len(combos)} combos exceeds max_combos={max_combos}")

    t0 = time.perf_counter()
    rows = []
    for combo in combos:
        params = dict(zip(names, combo))
        if _degenerate(params):
            continue
        sig = strategy.signal(prices, **params)
        result = run_backtest(
            prices, sig, config=config, symbol=symbol,
            strategy_name=strategy.name, params=params,
        )
        m = result.metrics()
        rows.append({**params, **{k: m[k] for k in
                    ("total_return", "cagr", "sharpe", "sortino", "max_drawdown",
                     "win_rate", "num_trades")}})
    elapsed = time.perf_counter() - t0

    table = pd.DataFrame(rows)
    if metric not in table.columns:
        raise ValueError(f"Unknown metric '{metric}'. Choices: {list(table.columns)}")
    table = table.sort_values(metric, ascending=False).reset_index(drop=True)
    best = (
        {n: _python_scalar(table.iloc[0][n]) for n in names} if len(table) else {}
    )

    return OptimizationResult(
        strategy_name=strategy.name,
        metric=metric,
        table=table,
        best_params=best,
        combos_tested=len(rows),
        elapsed_s=elapsed,
    )


def _python_scalar(value):
    """Convert numpy scalars to plain int/float for clean display."""
    v = value.item() if hasattr(value, "item") else value
    if isinstance(v, float) and v == int(v):
        return int(v)
    return v


def _degenerate(params: dict[str, Any]) -> bool:
    """Skip nonsensical combos like fast >= slow for crossover strategies."""
    if "fast" in params and "slow" in params and params["fast"] >= params["slow"]:
        return True
    if (
        "oversold" in params
        and "overbought" in params
        and params["oversold"] >= params["overbought"]
    ):
        return True
    return False
