"""Visual strategy builder: a declarative rule spec compiled to entry/exit signals.

A `RuleSpec` is the data behind the no-code builder UI. Each `Condition`
compares an indicator (the *left* operand) against either a constant or a
second indicator (the *right* operand) with an operator. Conditions are
chained with per-condition AND/OR joins, evaluated left to right.

The spec compiles to four boolean Series — long entry/exit and short
entry/exit — which the OHLC simulator (`backtest.simulate`) turns into trades.
Everything here is vectorized; no per-bar Python.

The INDICATORS / OPERATORS catalogs are the single source of truth shared
with the frontend (served at /api/builder/indicators) so the UI's dropdowns
and this resolver never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from shunkan.analytics import indicators as ta

# -- indicator catalog -------------------------------------------------------
# kind -> metadata. `period` flags whether the period field is meaningful;
# `band`/oscillator categories drive how the UI groups them.

INDICATORS: dict[str, dict] = {
    "RSI": {"label": "RSI", "period": True, "default": 14, "category": "momentum"},
    "STOCH_K": {"label": "Stochastic %K", "period": True, "default": 14, "category": "momentum"},
    "STOCH_D": {"label": "Stochastic %D", "period": True, "default": 14, "category": "momentum"},
    "CCI": {"label": "CCI", "period": True, "default": 20, "category": "momentum"},
    "WILLIAMS_R": {"label": "Williams %R", "period": True, "default": 14, "category": "momentum"},
    "MACD": {"label": "MACD line", "period": False, "default": 0, "category": "momentum"},
    "MACD_SIGNAL": {"label": "MACD signal", "period": False, "default": 0, "category": "momentum"},
    "ADX": {"label": "ADX", "period": True, "default": 14, "category": "trend"},
    "EMA": {"label": "EMA", "period": True, "default": 20, "category": "trend"},
    "SMA": {"label": "SMA", "period": True, "default": 50, "category": "trend"},
    "VWAP": {"label": "VWAP", "period": False, "default": 0, "category": "trend"},
    "BB_UPPER": {"label": "Bollinger upper", "period": True, "default": 20, "category": "volatility"},
    "BB_MIDDLE": {"label": "Bollinger middle", "period": True, "default": 20, "category": "volatility"},
    "BB_LOWER": {"label": "Bollinger lower", "period": True, "default": 20, "category": "volatility"},
    "ATR": {"label": "ATR", "period": True, "default": 14, "category": "volatility"},
    "OBV": {"label": "OBV", "period": False, "default": 0, "category": "volume"},
    "CLOSE": {"label": "Close price", "period": False, "default": 0, "category": "price"},
    "OPEN": {"label": "Open price", "period": False, "default": 0, "category": "price"},
    "HIGH": {"label": "High price", "period": False, "default": 0, "category": "price"},
    "LOW": {"label": "Low price", "period": False, "default": 0, "category": "price"},
}

# operator -> human label. Comparison + crossover operators.
OPERATORS: dict[str, str] = {
    "<": "less than",
    ">": "greater than",
    "<=": "at most",
    ">=": "at least",
    "cross_above": "crosses above",
    "cross_below": "crosses below",
}

_MIN_PERIOD, _MAX_PERIOD = 2, 200


@dataclass
class Operand:
    indicator: str
    period: int = 14

    @classmethod
    def from_dict(cls, d: dict) -> "Operand":
        kind = str(d.get("indicator", "")).upper()
        if kind not in INDICATORS:
            raise ValueError(f"Unknown indicator '{kind}'. Known: {', '.join(INDICATORS)}")
        period = int(d.get("period", INDICATORS[kind]["default"] or 14))
        if INDICATORS[kind]["period"] and not (_MIN_PERIOD <= period <= _MAX_PERIOD):
            raise ValueError(f"{kind} period {period} out of range [{_MIN_PERIOD}, {_MAX_PERIOD}]")
        return cls(indicator=kind, period=period)

    def describe(self) -> str:
        return f"{INDICATORS[self.indicator]['label']}" + (
            f"({self.period})" if INDICATORS[self.indicator]["period"] else ""
        )


@dataclass
class Condition:
    left: Operand
    op: str
    value: float | None = None  # constant right-hand side
    right: Operand | None = None  # indicator right-hand side (overrides value)
    join: str = "AND"  # how this condition combines with the previous one

    @classmethod
    def from_dict(cls, d: dict) -> "Condition":
        op = str(d.get("op", "")).strip()
        if op not in OPERATORS:
            raise ValueError(f"Unknown operator '{op}'. Known: {', '.join(OPERATORS)}")
        right = Operand.from_dict(d["right"]) if d.get("right") else None
        value = None if right is not None else _as_float(d.get("value"))
        if right is None and value is None:
            raise ValueError("condition needs either a numeric 'value' or a 'right' indicator")
        join = str(d.get("join", "AND")).upper()
        if join not in ("AND", "OR"):
            raise ValueError(f"join must be AND or OR, got '{join}'")
        return cls(left=Operand.from_dict(d["left"]), op=op, value=value, right=right, join=join)

    def describe(self) -> str:
        rhs = self.right.describe() if self.right is not None else f"{self.value:g}"
        return f"{self.left.describe()} {OPERATORS[self.op]} {rhs}"


@dataclass
class RuleSpec:
    long_entry: list[Condition] = field(default_factory=list)
    long_exit: list[Condition] = field(default_factory=list)
    short_entry: list[Condition] = field(default_factory=list)
    short_exit: list[Condition] = field(default_factory=list)
    direction: str = "long"  # long | short | both

    @classmethod
    def from_dict(cls, d: dict) -> "RuleSpec":
        def conds(key: str) -> list[Condition]:
            return [Condition.from_dict(c) for c in (d.get(key) or [])]

        direction = str(d.get("direction", "long")).lower()
        if direction not in ("long", "short", "both"):
            raise ValueError(f"direction must be long/short/both, got '{direction}'")
        spec = cls(
            long_entry=conds("long_entry"),
            long_exit=conds("long_exit"),
            short_entry=conds("short_entry"),
            short_exit=conds("short_exit"),
            direction=direction,
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        if self.direction in ("long", "both") and not self.long_entry:
            raise ValueError("long direction requires at least one long entry condition")
        if self.direction in ("short", "both") and not self.short_entry:
            raise ValueError("short direction requires at least one short entry condition")

    def describe(self) -> dict:
        return {
            "direction": self.direction,
            "long_entry": [c.describe() for c in self.long_entry],
            "long_exit": [c.describe() for c in self.long_exit],
            "short_entry": [c.describe() for c in self.short_entry],
            "short_exit": [c.describe() for c in self.short_exit],
        }


# -- resolution & evaluation -------------------------------------------------


def resolve(prices: pd.DataFrame, op: Operand) -> pd.Series:
    """Compute the Series for an operand from an OHLCV frame."""
    cols = {c.lower(): c for c in prices.columns}
    close = prices[cols["close"]].astype(float)
    kind, p = op.indicator, op.period

    if kind == "CLOSE":
        return close
    if kind == "OPEN":
        return prices[cols["open"]].astype(float)
    if kind == "HIGH":
        return prices[cols["high"]].astype(float)
    if kind == "LOW":
        return prices[cols["low"]].astype(float)
    if kind == "RSI":
        return ta.rsi(close, p)
    if kind == "EMA":
        return ta.ema(close, p)
    if kind == "SMA":
        return ta.sma(close, p)
    if kind == "MACD":
        return ta.macd(close)["macd"]
    if kind == "MACD_SIGNAL":
        return ta.macd(close)["signal"]
    if kind == "ATR":
        return ta.atr(prices, p)
    if kind == "ADX":
        return ta.adx(prices, p)
    if kind == "CCI":
        return ta.cci(prices, p)
    if kind == "WILLIAMS_R":
        return ta.williams_r(prices, p)
    if kind == "VWAP":
        return ta.vwap(prices)
    if kind == "OBV":
        return ta.obv(prices)
    if kind in ("BB_UPPER", "BB_MIDDLE", "BB_LOWER"):
        bands = ta.bollinger(close, p, 2.0)
        return bands[{"BB_UPPER": "upper", "BB_MIDDLE": "middle", "BB_LOWER": "lower"}[kind]]
    if kind == "STOCH_K":
        return ta.stochastic(prices, p)["k"]
    if kind == "STOCH_D":
        return ta.stochastic(prices, p)["d"]
    raise ValueError(f"Cannot resolve indicator '{kind}'")  # pragma: no cover


def eval_condition(prices: pd.DataFrame, cond: Condition) -> pd.Series:
    """Evaluate a single condition to a boolean Series (NaN inputs -> False)."""
    left = resolve(prices, cond.left)
    if cond.right is not None:
        right: pd.Series | float = resolve(prices, cond.right)
    else:
        right = float(cond.value)  # type: ignore[arg-type]

    if cond.op == "<":
        out = left < right
    elif cond.op == ">":
        out = left > right
    elif cond.op == "<=":
        out = left <= right
    elif cond.op == ">=":
        out = left >= right
    elif cond.op in ("cross_above", "cross_below"):
        prev_left = left.shift(1)
        prev_right = right.shift(1) if isinstance(right, pd.Series) else right
        if cond.op == "cross_above":
            out = (left > right) & (prev_left <= prev_right)
        else:
            out = (left < right) & (prev_left >= prev_right)
    else:  # pragma: no cover - guarded by Condition.from_dict
        raise ValueError(f"Unknown operator '{cond.op}'")

    # A comparison touching a NaN warm-up bar must be False, not NaN.
    return out.reindex(prices.index).fillna(False).astype(bool)


def combine(prices: pd.DataFrame, conds: list[Condition]) -> pd.Series:
    """Combine conditions left-to-right with each condition's AND/OR join."""
    if not conds:
        return pd.Series(False, index=prices.index)
    result = eval_condition(prices, conds[0])
    for cond in conds[1:]:
        nxt = eval_condition(prices, cond)
        result = (result & nxt) if cond.join == "AND" else (result | nxt)
    return result


@dataclass
class CompiledSignals:
    long_entry: pd.Series
    long_exit: pd.Series
    short_entry: pd.Series
    short_exit: pd.Series


def compile_spec(prices: pd.DataFrame, spec: RuleSpec) -> CompiledSignals:
    """Compile a RuleSpec against an OHLCV frame into boolean signal Series.

    Signals are evaluated on each bar's close; the simulator applies them on
    the next bar to avoid look-ahead. Sides not used by `direction` are empty
    (all-False) so the simulator simply never opens them.
    """
    empty = pd.Series(False, index=prices.index)
    long_on = spec.direction in ("long", "both")
    short_on = spec.direction in ("short", "both")
    return CompiledSignals(
        long_entry=combine(prices, spec.long_entry) if long_on else empty,
        long_exit=combine(prices, spec.long_exit) if long_on else empty,
        short_entry=combine(prices, spec.short_entry) if short_on else empty,
        short_exit=combine(prices, spec.short_exit) if short_on else empty,
    )


def _as_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
