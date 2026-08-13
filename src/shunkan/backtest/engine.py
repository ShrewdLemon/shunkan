"""Vectorized backtesting engine.

The engine takes a target-position signal series (-1, 0, +1 per bar) and
turns it into an equity curve in pure numpy — no per-bar Python loop — so a
10-year daily backtest completes in well under a millisecond and parameter
sweeps over thousands of combinations stay interactive.

Execution model:
- Signals are computed on bar close; fills happen on the NEXT bar's close
  (one-bar delay) to avoid look-ahead bias.
- Commission is charged per side as a fraction of traded notional.
- Slippage is modeled as an additional fractional cost per side.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from shunkan.backtest.result import BacktestResult, Trade


@dataclass
class BacktestConfig:
    initial_cash: float = 10_000.0
    commission: float = 0.0005  # 5 bps per side
    slippage: float = 0.0005  # 5 bps per side
    allow_short: bool = True


def run_backtest(
    prices: pd.DataFrame,
    signal: pd.Series,
    config: BacktestConfig | None = None,
    symbol: str = "?",
    strategy_name: str = "custom",
    params: dict | None = None,
) -> BacktestResult:
    """Run a vectorized backtest.

    prices: DataFrame with at least a 'close' column (case-insensitive).
    signal: target position per bar in {-1, 0, +1}, indexed like prices.
    """
    cfg = config or BacktestConfig()
    t0 = time.perf_counter()

    close = _close_series(prices)
    sig = signal.reindex(close.index).fillna(0.0).clip(-1, 1)
    if not cfg.allow_short:
        sig = sig.clip(lower=0)

    # Fill on next bar: position held during bar t is the signal from t-1.
    pos = sig.shift(1).fillna(0.0)

    bar_ret = close.pct_change().fillna(0.0).to_numpy()
    pos_arr = pos.to_numpy()

    # Cost charged when position changes, proportional to traded size.
    turnover = np.abs(np.diff(pos_arr, prepend=0.0))
    cost_rate = cfg.commission + cfg.slippage
    strat_ret = pos_arr * bar_ret - turnover * cost_rate

    equity_arr = cfg.initial_cash * np.cumprod(1.0 + strat_ret)
    equity = pd.Series(equity_arr, index=close.index, name="equity")
    returns = pd.Series(strat_ret, index=close.index, name="returns")

    trades = _extract_trades(close, pos, cost_rate)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    return BacktestResult(
        symbol=symbol,
        strategy_name=strategy_name,
        params=params or {},
        equity=equity,
        positions=pos,
        returns=returns,
        trades=trades,
        initial_cash=cfg.initial_cash,
        elapsed_ms=elapsed_ms,
    )


def _close_series(prices: pd.DataFrame) -> pd.Series:
    cols = {c.lower(): c for c in prices.columns}
    if "close" not in cols:
        raise ValueError(f"prices must contain a 'close' column, got {list(prices.columns)}")
    return prices[cols["close"]].astype(float)


def _extract_trades(close: pd.Series, pos: pd.Series, cost_rate: float) -> list[Trade]:
    """Identify round-trip trades from the position series."""
    pos_arr = pos.to_numpy()
    change_idx = np.flatnonzero(np.diff(pos_arr, prepend=0.0) != 0)

    trades: list[Trade] = []
    open_idx: int | None = None
    open_dir = 0
    for i in change_idx:
        new = pos_arr[i]
        if open_idx is not None and new != open_dir:
            trades.append(_make_trade(close, open_idx, i, open_dir, cost_rate))
            open_idx = None
            open_dir = 0
        if new != 0 and open_idx is None:
            open_idx = i
            open_dir = int(np.sign(new))
    if open_idx is not None and len(close) > 1:
        trades.append(_make_trade(close, open_idx, len(close) - 1, open_dir, cost_rate))
    return trades


def _make_trade(
    close: pd.Series, entry_i: int, exit_i: int, direction: int, cost_rate: float
) -> Trade:
    entry_price = float(close.iloc[entry_i])
    exit_price = float(close.iloc[exit_i])
    gross = direction * (exit_price / entry_price - 1.0)
    net = gross - 2.0 * cost_rate
    return Trade(
        entry_time=close.index[entry_i].to_pydatetime()
        if hasattr(close.index[entry_i], "to_pydatetime")
        else close.index[entry_i],
        exit_time=close.index[exit_i].to_pydatetime()
        if hasattr(close.index[exit_i], "to_pydatetime")
        else close.index[exit_i],
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        return_pct=net,
        bars_held=exit_i - entry_i,
    )
