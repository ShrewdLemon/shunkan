"""OHLC-aware, event-driven backtest simulator with intrabar stops/targets.

The vectorized close-only engine in `engine.py` can't model a stop-loss or
take-profit honestly — those are intrabar events that need the bar's high and
low. This simulator walks bar by bar so it can:

- enter on the bar *after* a signal (one-bar fill delay, no look-ahead),
- exit on an opposite/exit signal, a stop-loss, or a take-profit, whichever
  comes first within a bar, with gap-through-level fills,
- trail the stop, and apply session-hours / ATR-range / cooldown filters.

It still loops over bars in Python, but only does cheap scalar work per bar,
so an intraday history (tens of thousands of bars) stays in single-digit ms.
Output is a `BacktestResult`, identical in shape to the vectorized engine's,
so metrics, charts and reports are shared.

Fill model (documented so the equity curve can explain itself):
- Signal fills are at the next bar's OPEN.
- A stop/target gapped through at the open fills at the OPEN (you get the gap,
  not the level).
- If a bar's range touches both the stop and the target, the STOP is assumed
  hit first — the conservative assumption, since intrabar order is unknown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from shunkan.analytics import indicators as ta
from shunkan.backtest.builder import CompiledSignals
from shunkan.backtest.result import BacktestResult, Trade


@dataclass
class ExecConfig:
    initial_cash: float = 10_000.0
    commission: float = 0.0005  # per side, fraction of notional
    slippage: float = 0.0005  # per side
    # stop-loss / take-profit
    sl_mode: str = "none"  # none | percent | pips | atr
    sl_value: float = 0.0
    tp_mode: str = "none"  # none | percent | pips | atr
    tp_value: float = 0.0
    trailing: bool = False  # trail the stop in the trade's favour
    atr_period: int = 14
    pip_size: float = 1.0
    # filters
    session_start: str | None = None  # "HH:MM" (intraday only)
    session_end: str | None = None
    cooldown_bars: int = 0  # bars to wait after an exit before re-entering
    atr_min: float | None = None
    atr_max: float | None = None
    allow_short: bool = True
    params: dict = field(default_factory=dict)  # for reporting/provenance


def _levels(entry: float, direction: int, cfg: ExecConfig, atr_ref: float):
    """Return (stop_price, tp_price, trail_distance) for a new position."""
    def dist(mode: str, value: float) -> float | None:
        if mode == "none" or value <= 0:
            return None
        if mode == "percent":
            return entry * value / 100.0
        if mode == "pips":
            return value * cfg.pip_size
        if mode == "atr":
            return None if (atr_ref is None or np.isnan(atr_ref)) else value * atr_ref
        return None

    sl_dist = dist(cfg.sl_mode, cfg.sl_value)
    tp_dist = dist(cfg.tp_mode, cfg.tp_value)
    stop = tp = None
    if sl_dist is not None:
        stop = entry - sl_dist if direction > 0 else entry + sl_dist
    if tp_dist is not None:
        tp = entry + tp_dist if direction > 0 else entry - tp_dist
    return stop, tp, sl_dist


def _intrabar_exit(direction, o, h, l, stop, tp):
    """Resolve a stop/target hit within one bar. Returns (price, reason) or (None, None)."""
    if direction > 0:
        if stop is not None and o <= stop:
            return o, "stop"  # gapped down through the stop
        if tp is not None and o >= tp:
            return o, "target"  # gapped up through the target
        if stop is not None and l <= stop:
            return stop, "stop"  # stop assumed before target (conservative)
        if tp is not None and h >= tp:
            return tp, "target"
    else:
        if stop is not None and o >= stop:
            return o, "stop"
        if tp is not None and o <= tp:
            return o, "target"
        if stop is not None and h >= stop:
            return stop, "stop"
        if tp is not None and l <= tp:
            return tp, "target"
    return None, None


def _session_mask(index: pd.DatetimeIndex, start: str | None, end: str | None) -> np.ndarray:
    if not start or not end:
        return np.ones(len(index), dtype=bool)
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    start_min, end_min = sh * 60 + sm, eh * 60 + em
    minutes = np.array([t.hour * 60 + t.minute for t in index])
    if start_min <= end_min:
        return (minutes >= start_min) & (minutes <= end_min)
    return (minutes >= start_min) | (minutes <= end_min)  # session wraps midnight


def simulate(
    prices: pd.DataFrame,
    signals: CompiledSignals,
    cfg: ExecConfig | None = None,
    symbol: str = "?",
    strategy_name: str = "builder",
) -> BacktestResult:
    """Run the event-driven simulation. `signals` come from builder.compile_spec."""
    cfg = cfg or ExecConfig()
    t0 = time.perf_counter()

    cols = {c.lower(): c for c in prices.columns}
    o = prices[cols["open"]].to_numpy(float)
    h = prices[cols["high"]].to_numpy(float)
    low = prices[cols["low"]].to_numpy(float)
    c = prices[cols["close"]].to_numpy(float)
    idx = prices.index
    n = len(c)

    le = signals.long_entry.reindex(idx).fillna(False).to_numpy(bool)
    lx = signals.long_exit.reindex(idx).fillna(False).to_numpy(bool)
    se = (signals.short_entry.reindex(idx).fillna(False).to_numpy(bool)
          if cfg.allow_short else np.zeros(n, bool))
    sx = signals.short_exit.reindex(idx).fillna(False).to_numpy(bool)

    need_atr = cfg.sl_mode == "atr" or cfg.tp_mode == "atr" or \
        cfg.atr_min is not None or cfg.atr_max is not None
    atr_arr = (ta.atr(prices, cfg.atr_period).to_numpy(float)
               if need_atr else np.full(n, np.nan))

    session_ok = _session_mask(idx, cfg.session_start, cfg.session_end)
    cost_rate = cfg.commission + cfg.slippage

    ret = np.zeros(n)
    pos_arr = np.zeros(n)
    trades: list[Trade] = []

    def can_enter(i: int) -> bool:
        if not session_ok[i]:
            return False
        if cfg.atr_min is not None or cfg.atr_max is not None:
            a = atr_arr[i - 1]
            if np.isnan(a):
                return False
            if cfg.atr_min is not None and a < cfg.atr_min:
                return False
            if cfg.atr_max is not None and a > cfg.atr_max:
                return False
        return True

    i = 1
    last_exit = -(10**9)
    while i < n:
        direction = 0
        if (i - last_exit) > cfg.cooldown_bars and can_enter(i):
            if le[i - 1]:
                direction = 1
            elif se[i - 1]:
                direction = -1
        if direction == 0:
            i += 1
            continue

        entry_price = o[i]
        entry_i = i
        atr_ref = atr_arr[i - 1]
        stop, tp, trail_dist = _levels(entry_price, direction, cfg, atr_ref)
        trail_extreme = entry_price
        ret[i] -= cost_rate  # entry cost

        prev_mark = entry_price
        j = i
        exited = False
        while j < n:
            if cfg.trailing and trail_dist is not None:
                if direction > 0:
                    trail_extreme = max(trail_extreme, h[j])
                    stop = max(stop, trail_extreme - trail_dist) if stop is not None \
                        else trail_extreme - trail_dist
                else:
                    trail_extreme = min(trail_extreme, low[j])
                    stop = min(stop, trail_extreme + trail_dist) if stop is not None \
                        else trail_extreme + trail_dist

            exit_price = exit_reason = None
            if j > entry_i:  # signal exits act on the bar after the signal
                if (direction > 0 and lx[j - 1]) or (direction < 0 and sx[j - 1]):
                    exit_price, exit_reason = o[j], "signal"
            if exit_price is None:
                ep, rsn = _intrabar_exit(direction, o[j], h[j], low[j], stop, tp)
                if ep is not None:
                    exit_price, exit_reason = ep, rsn

            pos_arr[j] = direction
            if exit_price is not None:
                ret[j] += direction * (exit_price / prev_mark - 1.0) - cost_rate
                trades.append(Trade(
                    entry_time=_pytime(idx[entry_i]), exit_time=_pytime(idx[j]),
                    direction=direction, entry_price=entry_price, exit_price=exit_price,
                    return_pct=direction * (exit_price / entry_price - 1.0) - 2.0 * cost_rate,
                    bars_held=j - entry_i, exit_reason=exit_reason,
                ))
                last_exit = j
                i = j + 1
                exited = True
                break
            ret[j] += direction * (c[j] / prev_mark - 1.0)
            prev_mark = c[j]
            j += 1

        if not exited:  # ran past the end still in the trade
            trades.append(Trade(
                entry_time=_pytime(idx[entry_i]), exit_time=_pytime(idx[n - 1]),
                direction=direction, entry_price=entry_price, exit_price=c[n - 1],
                return_pct=direction * (c[n - 1] / entry_price - 1.0) - 2.0 * cost_rate,
                bars_held=n - 1 - entry_i, exit_reason="end-of-data",
            ))
            break

    equity = pd.Series(cfg.initial_cash * np.cumprod(1.0 + ret), index=idx, name="equity")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return BacktestResult(
        symbol=symbol, strategy_name=strategy_name, params=cfg.params,
        equity=equity, positions=pd.Series(pos_arr, index=idx, name="positions"),
        returns=pd.Series(ret, index=idx, name="returns"),
        trades=trades, initial_cash=cfg.initial_cash, elapsed_ms=elapsed_ms,
    )


def _pytime(ts):
    return ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
