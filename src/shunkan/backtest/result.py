"""Backtest result containers and report formatting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from shunkan.analytics import stats


@dataclass
class Trade:
    entry_time: datetime
    exit_time: datetime
    direction: int  # +1 long, -1 short
    entry_price: float
    exit_price: float
    return_pct: float
    bars_held: int
    exit_reason: str = "signal"  # signal | stop | target | end-of-data


@dataclass
class BacktestResult:
    symbol: str
    strategy_name: str
    params: dict[str, Any]
    equity: pd.Series  # equity curve, starts at initial_cash
    positions: pd.Series  # -1 / 0 / +1 per bar
    returns: pd.Series  # per-bar strategy returns (after costs)
    trades: list[Trade] = field(default_factory=list)
    initial_cash: float = 10_000.0
    elapsed_ms: float = 0.0

    @property
    def final_equity(self) -> float:
        return float(self.equity.iloc[-1]) if len(self.equity) else self.initial_cash

    @property
    def total_return(self) -> float:
        return self.final_equity / self.initial_cash - 1.0

    def metrics(self) -> dict[str, float]:
        trade_rets = [t.return_pct for t in self.trades]
        return {
            "total_return": self.total_return,
            "cagr": stats.cagr(self.equity),
            "sharpe": stats.sharpe(self.returns),
            "sortino": stats.sortino(self.returns),
            "calmar": stats.calmar(self.equity),
            "volatility": stats.volatility(self.returns),
            "max_drawdown": stats.max_drawdown(self.equity),
            "win_rate": stats.win_rate(trade_rets),
            "profit_factor": stats.profit_factor(trade_rets),
            "exposure": stats.exposure(self.positions),
            "num_trades": float(len(self.trades)),
        }

    def summary_rows(self) -> list[tuple[str, str]]:
        """Human-readable (label, value) pairs for tables."""
        m = self.metrics()
        pf = m["profit_factor"]
        return [
            ("Strategy", f"{self.strategy_name} {self.params}"),
            ("Bars", f"{len(self.equity):,}"),
            ("Initial equity", f"${self.initial_cash:,.2f}"),
            ("Final equity", f"${self.final_equity:,.2f}"),
            ("Total return", f"{m['total_return']:+.2%}"),
            ("CAGR", f"{m['cagr']:+.2%}"),
            ("Sharpe", f"{m['sharpe']:.2f}"),
            ("Sortino", f"{m['sortino']:.2f}" if m["sortino"] != float("inf") else "inf"),
            ("Calmar", f"{m['calmar']:.2f}"),
            ("Volatility (ann.)", f"{m['volatility']:.2%}"),
            ("Max drawdown", f"{m['max_drawdown']:.2%}"),
            ("Trades", f"{len(self.trades)}"),
            ("Win rate", f"{m['win_rate']:.1%}"),
            ("Profit factor", f"{pf:.2f}" if pf != float("inf") else "inf"),
            ("Exposure", f"{m['exposure']:.1%}"),
            ("Engine time", f"{self.elapsed_ms:.1f} ms"),
        ]
