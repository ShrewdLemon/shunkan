"""Shun Script — a tiny, safe, vectorized strategy DSL.

Pine-flavored, Python-shaped. A script is a sequence of assignments and
calls over OHLCV series; everything evaluates to whole pandas Series in one
pass (no per-bar loop), then the signal runs through the same backtest
engine as everything else — same costs, same next-bar fills, same honesty.

    fast = ema(close, 12)
    slow = ema(close, 26)
    plot(fast, color="amber", title="EMA 12")
    plot(slow, color="blue")
    long_when(cross_above(fast, slow))
    short_when(cross_below(fast, slow))

Safety: the source is parsed with `ast` and only a whitelist of node types
survives — no imports, no attribute access, no subscripts, no loops, no
lambdas, no dunder anything. Unknown names and functions raise ScriptError
with the line number.
"""

from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from shunkan.analytics import indicators as ta

MAX_SOURCE_CHARS = 20_000
MAX_STATEMENTS = 200

PLOT_COLORS = {"amber": "#f0a826", "blue": "#58a6ff", "green": "#2ebd85",
               "red": "#f1564b", "white": "#dde1e8", "gray": "#5d6470"}


class ScriptError(ValueError):
    def __init__(self, msg: str, line: int | None = None):
        super().__init__(f"line {line}: {msg}" if line else msg)
        self.line = line


@dataclass
class ScriptResult:
    plots: list[dict] = field(default_factory=list)     # {title,color,panel,values}
    hlines: list[dict] = field(default_factory=list)    # {y,color,panel}
    signal: pd.Series | None = None                     # target position per bar
    variables: dict[str, float] = field(default_factory=dict)  # last values
    elapsed_ms: float = 0.0


_ALLOWED_EXPR = (ast.Name, ast.Constant, ast.BinOp, ast.UnaryOp, ast.Compare,
                 ast.BoolOp, ast.Call, ast.keyword,
                 ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.USub,
                 ast.UAdd, ast.Gt, ast.Lt, ast.GtE, ast.LtE, ast.Eq, ast.NotEq,
                 ast.And, ast.Or, ast.Not, ast.Load, ast.Store)


def _series(x, like: pd.Series) -> pd.Series:
    if isinstance(x, pd.Series):
        return x
    return pd.Series(float(x), index=like.index)


def _cross(a, b, like, above: bool) -> pd.Series:
    a, b = _series(a, like), _series(b, like)
    prev = a.shift(1) <= b.shift(1) if above else a.shift(1) >= b.shift(1)
    now = a > b if above else a < b
    return (prev & now).fillna(False)


def _build_env(ohlc: pd.DataFrame, result: ScriptResult):
    cols = {c.lower(): c for c in ohlc.columns}
    close = ohlc[cols["close"]].astype(float)
    env: dict[str, object] = {
        "close": close,
        "open": ohlc[cols["open"]].astype(float) if "open" in cols else close,
        "high": ohlc[cols["high"]].astype(float) if "high" in cols else close,
        "low": ohlc[cols["low"]].astype(float) if "low" in cols else close,
        "volume": ohlc[cols["volume"]].astype(float) if "volume" in cols
        else pd.Series(0.0, index=close.index),
    }
    conds = {"long": None, "short": None, "exit": None}

    def plot(series, color="white", title="", panel=0):
        s = _series(series, close)
        result.plots.append({
            "title": str(title) or f"plot{len(result.plots) + 1}",
            "color": PLOT_COLORS.get(str(color), str(color)),
            "panel": int(panel),
            "values": s.astype(float),
        })

    def hline(y, color="gray", panel=0):
        result.hlines.append({"y": float(y),
                              "color": PLOT_COLORS.get(str(color), str(color)),
                              "panel": int(panel)})

    def _when(key):
        def f(cond):
            c = _series(cond, close).astype(bool)
            conds[key] = c if conds[key] is None else (conds[key] | c)
        return f

    funcs = {
        # indicators (series-first, like the analytics module)
        "sma": lambda s, n: ta.sma(_series(s, close), int(n)),
        "ema": lambda s, n: ta.ema(_series(s, close), int(n)),
        "rsi": lambda s=None, n=14: ta.rsi(_series(s if s is not None else close, close), int(n)),
        "macd_line": lambda s=None, fast=12, slow=26: ta.ema(_series(s if s is not None else close, close), int(fast)) - ta.ema(_series(s if s is not None else close, close), int(slow)),
        "atr": lambda n=14: ta.atr(ohlc, int(n)),
        "vwap": lambda: ta.vwap(ohlc),
        "momentum": lambda s, n=63: ta.momentum(_series(s, close), int(n)),
        "stdev": lambda s, n: _series(s, close).rolling(int(n)).std(),
        "highest": lambda s, n: _series(s, close).rolling(int(n)).max(),
        "lowest": lambda s, n: _series(s, close).rolling(int(n)).min(),
        "shift": lambda s, n=1: _series(s, close).shift(int(n)),
        "abs": lambda s: _series(s, close).abs() if isinstance(s, pd.Series) else abs(s),
        "change": lambda s, n=1: _series(s, close).pct_change(int(n)),
        "cross_above": lambda a, b: _cross(a, b, close, True),
        "cross_below": lambda a, b: _cross(a, b, close, False),
        # bollinger helpers
        "bb_upper": lambda s=None, n=20, k=2.0: ta.bollinger(_series(s if s is not None else close, close), int(n), float(k))["upper"],
        "bb_lower": lambda s=None, n=20, k=2.0: ta.bollinger(_series(s if s is not None else close, close), int(n), float(k))["lower"],
        # output + strategy verbs
        "plot": plot, "hline": hline,
        "long_when": _when("long"), "short_when": _when("short"),
        "exit_when": _when("exit"),
    }
    return env, funcs, conds


class _Evaluator(ast.NodeVisitor):
    def __init__(self, env, funcs):
        self.env, self.funcs = env, funcs

    def eval(self, node):
        for sub in ast.walk(node):
            if not isinstance(sub, _ALLOWED_EXPR):
                raise ScriptError(f"'{type(sub).__name__}' is not allowed",
                                  getattr(sub, "lineno", None))
        return self._e(node)

    def _e(self, n):
        if isinstance(n, ast.Constant):
            if isinstance(n.value, (int, float, str, bool)):
                return n.value
            raise ScriptError(f"constant {n.value!r} not allowed", n.lineno)
        if isinstance(n, ast.Name):
            if n.id in self.env:
                return self.env[n.id]
            raise ScriptError(f"unknown name '{n.id}'", n.lineno)
        if isinstance(n, ast.BinOp):
            a, b = self._e(n.left), self._e(n.right)
            ops = {ast.Add: lambda: a + b, ast.Sub: lambda: a - b,
                   ast.Mult: lambda: a * b, ast.Div: lambda: a / b,
                   ast.Mod: lambda: a % b, ast.Pow: lambda: a ** b}
            return ops[type(n.op)]()
        if isinstance(n, ast.UnaryOp):
            v = self._e(n.operand)
            if isinstance(n.op, ast.USub):
                return -v
            if isinstance(n.op, ast.Not):
                return ~v if isinstance(v, pd.Series) else (not v)
            return +v
        if isinstance(n, ast.Compare):
            if len(n.ops) != 1:
                raise ScriptError("chained comparisons not supported", n.lineno)
            a, b = self._e(n.left), self._e(n.comparators[0])
            ops = {ast.Gt: lambda: a > b, ast.Lt: lambda: a < b,
                   ast.GtE: lambda: a >= b, ast.LtE: lambda: a <= b,
                   ast.Eq: lambda: a == b, ast.NotEq: lambda: a != b}
            return ops[type(n.ops[0])]()
        if isinstance(n, ast.BoolOp):
            vals = [self._e(v) for v in n.values]
            out = vals[0]
            for v in vals[1:]:
                out = (out & v) if isinstance(n.op, ast.And) else (out | v)
            return out
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name):
                raise ScriptError("only plain function calls allowed", n.lineno)
            name = n.func.id
            if name not in self.funcs:
                raise ScriptError(f"unknown function '{name}'", n.lineno)
            args = [self._e(a) for a in n.args]
            kwargs = {k.arg: self._e(k.value) for k in n.keywords if k.arg}
            try:
                return self.funcs[name](*args, **kwargs)
            except ScriptError:
                raise
            except Exception as exc:
                raise ScriptError(f"{name}(): {exc}", n.lineno) from exc
        raise ScriptError(f"'{type(n).__name__}' not allowed", getattr(n, "lineno", None))


def run_script(source: str, ohlc: pd.DataFrame) -> ScriptResult:
    """Parse + evaluate a Shun Script over an OHLCV frame."""
    t0 = time.perf_counter()
    if len(source) > MAX_SOURCE_CHARS:
        raise ScriptError(f"script too long (> {MAX_SOURCE_CHARS} chars)")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ScriptError(f"syntax error: {exc.msg}", exc.lineno) from exc
    if len(tree.body) > MAX_STATEMENTS:
        raise ScriptError(f"too many statements (> {MAX_STATEMENTS})")

    result = ScriptResult()
    env, funcs, conds = _build_env(ohlc, result)
    ev = _Evaluator(env, funcs)

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                raise ScriptError("assign to a single plain name", stmt.lineno)
            name = stmt.targets[0].id
            if name in funcs or name in ("close", "open", "high", "low", "volume"):
                raise ScriptError(f"cannot reassign built-in '{name}'", stmt.lineno)
            env[name] = ev.eval(stmt.value)
        elif isinstance(stmt, ast.Expr):
            ev.eval(stmt.value)
        else:
            raise ScriptError(
                f"only assignments and calls allowed, not {type(stmt).__name__}",
                stmt.lineno)

    close = env["close"]
    if any(c is not None for c in conds.values()):
        sig = pd.Series(np.nan, index=close.index)
        if conds["long"] is not None:
            sig[conds["long"]] = 1.0
        if conds["short"] is not None:
            sig[conds["short"]] = -1.0
        if conds["exit"] is not None:
            sig[conds["exit"].astype(bool)] = 0.0
        result.signal = sig.ffill().fillna(0.0)

    for k, v in env.items():
        if isinstance(v, pd.Series) and k not in ("open", "high", "low", "volume"):
            last = v.iloc[-1]
            if isinstance(last, (bool, np.bool_)):
                result.variables[k] = bool(last)
            elif np.isfinite(last):
                result.variables[k] = float(last)
    result.elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return result
