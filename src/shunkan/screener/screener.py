"""Rule-based technical screener.

Computes a metrics row per symbol (price, returns, RSI, distance from highs,
volatility, volume surge, SMA trend) and filters it with simple expressions:

    rsi<30            oversold
    ret_1mo>0.05      up more than 5% in a month
    above_sma200      price above its 200-day SMA
    vol_surge>2       volume 2x its 3-month average

Multiple rules are AND-ed: ``run_screen(universe, ["rsi<35", "above_sma50"])``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from shunkan.analytics import indicators as ta

if TYPE_CHECKING:
    from shunkan.data.provider import DataProvider

UNIVERSES: dict[str, list[str]] = {
    # Indian universes (bare NSE names — resolved to .NS automatically)
    "nifty50": ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC", "LT",
                "SBIN", "AXISBANK", "KOTAKBANK", "BHARTIARTL", "ASIANPAINT",
                "MARUTI", "TITAN", "SUNPHARMA", "M&M", "ULTRACEMCO",
                "BAJFINANCE", "NTPC", "POWERGRID"],
    "banks": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK",
              "INDUSINDBK", "BANKBARODA", "PNB", "FEDERALBNK", "IDFCFIRSTB"],
    "it": ["TCS", "INFY", "HCLTECH", "WIPRO", "TECHM", "LTIM", "PERSISTENT", "COFORGE"],
    "fno": ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN",
            "BHARTIARTL", "LT", "BAJFINANCE", "MARUTI", "TITAN", "ADANIENT",
            "TATASTEEL", "HINDALCO", "AXISBANK", "DLF", "HAL", "BEL"],
    # Global universes
    "mega": ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRK-B", "AVGO", "LLY"],
    "tech": ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AVGO", "ORCL", "CRM", "AMD", "ADBE",
             "INTC", "CSCO", "QCOM", "TXN", "NOW", "UBER", "SHOP", "PLTR", "SNOW", "NET"],
    "semis": ["NVDA", "AMD", "AVGO", "TSM", "INTC", "QCOM", "TXN", "MU", "AMAT", "ASML",
              "LRCX", "KLAC", "MRVL", "ON", "ADI"],
    "finance": ["JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW", "AXP", "V", "MA", "PYPL"],
    "energy": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY", "HAL"],
    "etf": ["SPY", "QQQ", "IWM", "DIA", "VTI", "GLD", "SLV", "TLT", "HYG", "XLE", "XLF", "XLK"],
}

# metric name -> (description, higher_is_better) — used for table ordering hints
METRICS = {
    "price": "Last close",
    "ret_1d": "1-day return",
    "ret_1w": "1-week return",
    "ret_1mo": "1-month return",
    "ret_3mo": "3-month return",
    "rsi": "RSI(14)",
    "vol_ann": "Annualized volatility",
    "from_high": "Distance from 6-month high (negative = below)",
    "vol_surge": "Volume vs 3-month average",
    "above_sma50": "Price above SMA(50): 1/0",
    "above_sma200": "Price above SMA(200): 1/0",
}

_RULE_RE = re.compile(r"^\s*([a-z_0-9]+)\s*(<=|>=|<|>|==|=)?\s*(-?[\d.]+)?\s*$")


@dataclass
class ScreenResult:
    table: pd.DataFrame  # index: symbol, columns: metrics; only passing rows
    rules: list[str]
    universe: list[str]
    errors: dict[str, str]  # symbol -> error message for fetch failures


def compute_metrics(hist: pd.DataFrame) -> dict[str, float]:
    close = hist["close"]
    rsi_series = ta.rsi(close, 14)
    sma50 = ta.sma(close, 50)
    sma200 = ta.sma(close, 200)
    ret = close.pct_change()
    vol3mo = hist["volume"].rolling(63, min_periods=10).mean()

    def last(series: pd.Series, default: float = np.nan) -> float:
        s = series.dropna()
        return float(s.iloc[-1]) if len(s) else default

    price = last(close)
    high_6mo = float(close.tail(126).max()) if len(close) else np.nan
    return {
        "price": price,
        "ret_1d": price / float(close.iloc[-2]) - 1.0 if len(close) > 2 else np.nan,
        "ret_1w": price / float(close.iloc[-6]) - 1.0 if len(close) > 6 else np.nan,
        "ret_1mo": price / float(close.iloc[-22]) - 1.0 if len(close) > 22 else np.nan,
        "ret_3mo": price / float(close.iloc[-64]) - 1.0 if len(close) > 64 else np.nan,
        "rsi": last(rsi_series),
        "vol_ann": float(ret.std(ddof=1) * np.sqrt(252)) if len(ret.dropna()) > 2 else np.nan,
        "from_high": price / high_6mo - 1.0 if high_6mo and not np.isnan(high_6mo) else np.nan,
        "vol_surge": float(hist["volume"].iloc[-1] / vol3mo.iloc[-1])
        if len(vol3mo.dropna()) and vol3mo.iloc[-1] > 0
        else np.nan,
        "above_sma50": float(price > last(sma50, np.inf)),
        "above_sma200": float(price > last(sma200, np.inf)),
    }


def parse_rule(rule: str) -> tuple[str, str, float]:
    m = _RULE_RE.match(rule.lower())
    if not m:
        raise ValueError(f"Cannot parse rule '{rule}'. Example: rsi<30")
    metric, op, value = m.groups()
    if metric not in METRICS:
        raise ValueError(f"Unknown metric '{metric}'. Choices: {', '.join(METRICS)}")
    if op is None and value is None:
        # Bare boolean metric, e.g. "above_sma200"
        return metric, ">", 0.5
    if op is None or value is None:
        raise ValueError(f"Rule '{rule}' needs both an operator and a value")
    if op == "=":
        op = "=="
    return metric, op, float(value)


def run_screen(
    provider: "DataProvider",
    universe: list[str],
    rules: list[str],
    period: str = "1y",
) -> ScreenResult:
    parsed = [parse_rule(r) for r in rules]
    rows: dict[str, dict[str, float]] = {}
    errors: dict[str, str] = {}
    for sym in universe:
        try:
            hist = provider.history(sym, period=period, interval="1d")
            rows[sym.upper()] = compute_metrics(hist)
        except Exception as exc:
            errors[sym.upper()] = str(exc)

    table = pd.DataFrame.from_dict(rows, orient="index")
    if len(table):
        mask = pd.Series(True, index=table.index)
        for metric, op, value in parsed:
            col = table[metric]
            if op == "<":
                mask &= col < value
            elif op == "<=":
                mask &= col <= value
            elif op == ">":
                mask &= col > value
            elif op == ">=":
                mask &= col >= value
            else:
                mask &= col == value
        table = table[mask.fillna(False)]
        sort_col = parsed[0][0] if parsed else "ret_1mo"
        ascending = parsed[0][1] in ("<", "<=") if parsed else False
        table = table.sort_values(sort_col, ascending=ascending)

    return ScreenResult(table=table, rules=rules, universe=universe, errors=errors)
