"""Performance statistics for equity curves and return streams."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def cagr(equity: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    """Compound annual growth rate of an equity curve."""
    eq = equity.dropna()
    if len(eq) < 2 or eq.iloc[0] <= 0:
        return 0.0
    total = eq.iloc[-1] / eq.iloc[0]
    if total <= 0:
        return -1.0
    years = (len(eq) - 1) / periods_per_year
    if years <= 0:
        return 0.0
    return total ** (1.0 / years) - 1.0


def sharpe(
    returns: pd.Series,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS,
) -> float:
    """Annualized Sharpe ratio from per-period returns."""
    r = returns.dropna()
    if len(r) < 2:
        return 0.0
    excess = r - risk_free_rate / periods_per_year
    sd = excess.std(ddof=1)
    # Guard with an epsilon scaled to the mean: constant return streams can
    # produce sd ~ 1e-18 from float roundoff rather than exactly zero.
    if math.isnan(sd) or sd <= max(abs(excess.mean()), 1.0) * 1e-12:
        return 0.0
    return float(excess.mean() / sd * math.sqrt(periods_per_year))


def sortino(
    returns: pd.Series,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS,
) -> float:
    """Annualized Sortino ratio (downside deviation in the denominator)."""
    r = returns.dropna()
    if len(r) < 2:
        return 0.0
    excess = r - risk_free_rate / periods_per_year
    downside = excess[excess < 0]
    if len(downside) == 0:
        return float("inf") if excess.mean() > 0 else 0.0
    dd = math.sqrt((downside**2).mean())
    if dd <= max(abs(excess.mean()), 1.0) * 1e-12:
        return 0.0
    return float(excess.mean() / dd * math.sqrt(periods_per_year))


def max_drawdown(equity: pd.Series) -> float:
    """Maximum drawdown as a negative fraction (e.g. -0.35)."""
    eq = equity.dropna()
    if len(eq) < 2:
        return 0.0
    peak = eq.cummax()
    return float((eq / peak - 1.0).min())


def calmar(equity: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    """Calmar ratio: CAGR / |max drawdown|."""
    mdd = max_drawdown(equity)
    if mdd == 0:
        return 0.0
    return cagr(equity, periods_per_year) / abs(mdd)


def volatility(returns: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    """Annualized volatility of per-period returns."""
    r = returns.dropna()
    if len(r) < 2:
        return 0.0
    return float(r.std(ddof=1) * math.sqrt(periods_per_year))


def win_rate(trade_returns: list[float] | np.ndarray | pd.Series) -> float:
    """Fraction of trades with positive return."""
    arr = np.asarray(trade_returns, dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size == 0:
        return 0.0
    return float((arr > 0).mean())


def profit_factor(trade_returns: list[float] | np.ndarray | pd.Series) -> float:
    """Gross profit / gross loss across trades."""
    arr = np.asarray(trade_returns, dtype=float)
    arr = arr[~np.isnan(arr)]
    gains = arr[arr > 0].sum()
    losses = -arr[arr < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return float(gains / losses)


def exposure(positions: pd.Series) -> float:
    """Fraction of bars with a nonzero position."""
    p = positions.dropna()
    if len(p) == 0:
        return 0.0
    return float((p != 0).mean())
