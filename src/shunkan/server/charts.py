"""Chart indicator computation for the CHT view.

Reuses the vectorized indicators in `analytics.indicators` and shapes them into
chart-ready line/histogram series (warm-up NaNs omitted so lightweight-charts
renders clean gaps) plus a provenance record per indicator — every line on the
chart can explain how it was made, same discipline as the rest of Shunkan.

`CHART_INDICATORS` is the single source of truth shared with the frontend
(served via /api/chart/indicators) so the UI's indicator menu and this compute
path never drift. `pane: "price"` overlays on the candles; `pane: "lower"`
renders in its own oscillator pane.
"""

from __future__ import annotations

import math

import pandas as pd

from shunkan.analytics import indicators as ta
from shunkan.provenance import prov

# kind -> metadata. period=False means the period field is ignored.
CHART_INDICATORS: dict[str, dict] = {
    "SMA": {"label": "SMA", "pane": "price", "period": True, "default": 20},
    "EMA": {"label": "EMA", "pane": "price", "period": True, "default": 50},
    "BB": {"label": "Bollinger", "pane": "price", "period": True, "default": 20},
    "VWAP": {"label": "VWAP", "pane": "price", "period": False, "default": 0},
    "RSI": {"label": "RSI", "pane": "lower", "period": True, "default": 14,
            "range": [0, 100], "guides": [30, 70]},
    "MACD": {"label": "MACD", "pane": "lower", "period": False, "default": 0},
    "ATR": {"label": "ATR", "pane": "lower", "period": True, "default": 14},
    "ADX": {"label": "ADX", "pane": "lower", "period": True, "default": 14,
            "range": [0, 100], "guides": [25]},
    "STOCH": {"label": "Stochastic", "pane": "lower", "period": True, "default": 14,
              "range": [0, 100], "guides": [20, 80]},
    "CCI": {"label": "CCI", "pane": "lower", "period": True, "default": 20,
            "guides": [-100, 100]},
    "WILLR": {"label": "Williams %R", "pane": "lower", "period": True, "default": 14,
              "range": [-100, 0], "guides": [-80, -20]},
    "OBV": {"label": "OBV", "pane": "lower", "period": False, "default": 0},
}

_PALETTE = ["#f0a826", "#58a6ff", "#2ebd85", "#bc8cff", "#f1564b", "#56d4dd"]


def _close(df: pd.DataFrame) -> pd.Series:
    cols = {c.lower(): c for c in df.columns}
    return df[cols["close"]].astype(float)


def _line(times: list[int], series: pd.Series) -> list[dict]:
    """{time,value} points, skipping NaN/inf warm-up bars."""
    vals = series.to_numpy(dtype=float)
    out: list[dict] = []
    for i in range(len(vals)):
        v = vals[i]
        if math.isnan(v) or math.isinf(v):
            continue
        out.append({"time": times[i], "value": float(v)})
    return out


def parse_specs(specs: str) -> list[tuple[str, int]]:
    """Parse 'sma:20,rsi:14,macd' -> [('SMA',20),('RSI',14),('MACD',0)]."""
    out: list[tuple[str, int]] = []
    for chunk in specs.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        kind, _, p = chunk.partition(":")
        kind = kind.strip().upper()
        if kind not in CHART_INDICATORS:
            continue
        meta = CHART_INDICATORS[kind]
        period = int(p) if (p.strip().isdigit() and meta["period"]) else (meta["default"] or 0)
        out.append((kind, period))
    return out


def compute_indicator(
    df: pd.DataFrame, kind: str, period: int, times: list[int], source: str
) -> dict:
    """Compute one chart indicator into chart-ready series + provenance."""
    meta = CHART_INDICATORS[kind]
    close = _close(df)
    label = f"{meta['label']}{f' {period}' if meta['period'] else ''}"
    base = {
        "id": f"{kind}:{period}", "kind": kind, "label": label,
        "pane": meta["pane"], "range": meta.get("range"), "guides": meta.get("guides"),
        "lines": [], "hist": None,
    }

    if kind == "SMA":
        base["lines"] = [_named(label, _PALETTE[0], _line(times, ta.sma(close, period)))]
        formula, inputs = "mean(close, N bars)", {"window": period}
    elif kind == "EMA":
        base["lines"] = [_named(label, _PALETTE[1], _line(times, ta.ema(close, period)))]
        formula, inputs = "exponential MA(close), span N", {"span": period}
    elif kind == "VWAP":
        base["lines"] = [_named("VWAP", _PALETTE[0], _line(times, ta.vwap(df)))]
        formula, inputs = "Σ(typical·vol)/Σvol, cumulative", {"typical": "(H+L+C)/3"}
    elif kind == "BB":
        b = ta.bollinger(close, period, 2.0)
        base["lines"] = [
            _named("Upper", _PALETTE[4], _line(times, b["upper"])),
            _named("Mid", _PALETTE[0], _line(times, b["middle"])),
            _named("Lower", _PALETTE[2], _line(times, b["lower"])),
        ]
        formula, inputs = "SMA ± 2σ over N bars", {"window": period, "num_std": 2.0}
    elif kind == "RSI":
        base["lines"] = [_named(label, _PALETTE[3], _line(times, ta.rsi(close, period)))]
        formula, inputs = "100 − 100/(1+RS), Wilder", {"window": period}
    elif kind == "MACD":
        m = ta.macd(close)
        base["lines"] = [
            _named("MACD", _PALETTE[1], _line(times, m["macd"])),
            _named("Signal", _PALETTE[4], _line(times, m["signal"])),
        ]
        base["hist"] = _line(times, m["histogram"])
        formula, inputs = "EMA12−EMA26, signal EMA9", {"fast": 12, "slow": 26, "signal": 9}
    elif kind == "ATR":
        base["lines"] = [_named(label, _PALETTE[5], _line(times, ta.atr(df, period)))]
        formula, inputs = "Wilder MA of true range", {"window": period}
    elif kind == "ADX":
        base["lines"] = [_named(label, _PALETTE[0], _line(times, ta.adx(df, period)))]
        formula, inputs = "Wilder smoothed DX", {"window": period}
    elif kind == "STOCH":
        s = ta.stochastic(df, period, 3)
        base["lines"] = [
            _named("%K", _PALETTE[1], _line(times, s["k"])),
            _named("%D", _PALETTE[4], _line(times, s["d"])),
        ]
        formula, inputs = "%K=(C−Ln)/(Hn−Ln); %D=SMA3", {"k_window": period, "d_window": 3}
    elif kind == "CCI":
        base["lines"] = [_named(label, _PALETTE[3], _line(times, ta.cci(df, period)))]
        formula, inputs = "(TP−SMA)/(0.015·MAD)", {"window": period}
    elif kind == "WILLR":
        base["lines"] = [_named(label, _PALETTE[5], _line(times, ta.williams_r(df, period)))]
        formula, inputs = "−100·(Hn−C)/(Hn−Ln)", {"window": period}
    elif kind == "OBV":
        base["lines"] = [_named("OBV", _PALETTE[2], _line(times, ta.obv(df)))]
        formula, inputs = "cumulative volume signed by close move", {}
    else:  # pragma: no cover - guarded by parse_specs
        raise ValueError(f"unknown chart indicator {kind}")

    base["prov"] = prov(formula, inputs, source, method=f"{label} over the loaded bars")
    return base


def _named(title: str, color: str, data: list[dict]) -> dict:
    return {"title": title, "color": color, "data": data}
