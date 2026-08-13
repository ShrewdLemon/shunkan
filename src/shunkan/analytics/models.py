"""Exotic model engines — Heston, Kalman, attention analogs. Pure numpy.

Built for daily research use, not demo theater: Heston anchors v0 to the
live ATM IV, Kalman runs on the symbol's actual closes, and the attention
matrix is *untrained* state-similarity (kernel attention) — analog-days
research, labeled as such. Every result says what is market data and what
is model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from shunkan.analytics import indicators as ta


# ---------------------------------------------------------------- Heston

@dataclass
class HestonFan:
    spot: float
    v0: float
    kappa: float
    theta: float
    xi: float
    rho: float
    feller_ok: bool
    horizon: int
    n_paths: int
    days: np.ndarray
    display_paths: np.ndarray      # (n_display, h) prices
    display_vols: np.ndarray       # (n_display, h) instantaneous vol (ann.)
    envelope: dict = field(repr=False, default=None)
    terminal_bins: np.ndarray = field(repr=False, default=None)
    terminal_freq: np.ndarray = field(repr=False, default=None)
    prob_up: float = 0.0
    elapsed_ms: float = 0.0


def heston_fan(
    spot: float, atm_iv: float, horizon: int = 120, n_paths: int = 2000,
    kappa: float = 2.0, theta: float | None = None, xi: float = 0.6,
    rho: float = -0.7, mu: float = 0.0, n_display: int = 40, seed: int = 7,
) -> HestonFan:
    """Heston paths via full-truncation Euler. v0 = atm_iv² (live anchor)."""
    t0 = time.perf_counter()
    v0 = atm_iv ** 2
    theta = v0 if theta is None else theta
    dt = 1.0 / 252.0
    rng = np.random.default_rng(seed)
    z1 = rng.standard_normal((n_paths, horizon))
    z2 = rho * z1 + np.sqrt(1 - rho ** 2) * rng.standard_normal((n_paths, horizon))

    S = np.full(n_paths, float(spot))
    V = np.full(n_paths, v0)
    prices = np.empty((n_paths, horizon))
    vols = np.empty((n_paths, horizon))
    for t in range(horizon):
        Vp = np.maximum(V, 0.0)                       # full truncation
        S = S * np.exp((mu - 0.5 * Vp) * dt + np.sqrt(Vp * dt) * z1[:, t])
        V = V + kappa * (theta - Vp) * dt + xi * np.sqrt(Vp * dt) * z2[:, t]
        prices[:, t] = S
        vols[:, t] = np.sqrt(Vp)

    terminal = prices[:, -1]
    order = np.argsort(terminal)
    pick = order[np.linspace(0, n_paths - 1, n_display).astype(int)]
    freq, edges = np.histogram(terminal, bins=31)
    return HestonFan(
        spot=spot, v0=v0, kappa=kappa, theta=theta, xi=xi, rho=rho,
        feller_ok=bool(2 * kappa * theta >= xi ** 2),
        horizon=horizon, n_paths=n_paths,
        days=np.arange(1, horizon + 1),
        display_paths=prices[pick], display_vols=vols[pick],
        envelope={f"p{p}": np.percentile(prices, p, axis=0) for p in (5, 50, 95)},
        terminal_bins=(edges[:-1] + edges[1:]) / 2.0,
        terminal_freq=freq / freq.max(),
        prob_up=float((terminal > spot).mean()),
        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
    )


# ---------------------------------------------------------------- Kalman

@dataclass
class KalmanTrend:
    level: np.ndarray
    slope: np.ndarray              # per-bar drift estimate
    band: np.ndarray               # ±2σ level uncertainty
    innovation_z: np.ndarray       # standardized surprise per bar
    q: float
    r: float
    last_z: float = 0.0
    elapsed_ms: float = 0.0


def kalman_trend(closes: np.ndarray, q: float = 1e-5, r: float = 1e-2) -> KalmanTrend:
    """Local-level + trend Kalman filter on log price.

    State [level, slope], F=[[1,1],[0,1]], observe level. q scales process
    noise (how fast the trend may bend), r measurement noise (how much a
    single close is trusted). Innovation z-scores flag genuine surprises.
    """
    t0 = time.perf_counter()
    y = np.log(np.asarray(closes, dtype=np.float64))
    n = len(y)
    if n < 30:
        raise ValueError(f"Need 30+ closes, got {n}")
    F = np.array([[1.0, 1.0], [0.0, 1.0]])
    Q = q * np.array([[0.25, 0.5], [0.5, 1.0]])
    x = np.array([y[0], 0.0])
    P = np.eye(2)
    level = np.empty(n); slope = np.empty(n); band = np.empty(n); zs = np.empty(n)
    for t in range(n):
        x = F @ x
        P = F @ P @ F.T + Q
        innov = y[t] - x[0]
        s = P[0, 0] + r
        zs[t] = innov / np.sqrt(s)
        K = P[:, 0] / s
        x = x + K * innov
        P = P - np.outer(K, P[0, :])
        level[t], slope[t] = x
        band[t] = 2.0 * np.sqrt(max(P[0, 0], 0.0))
    return KalmanTrend(
        level=np.exp(level), slope=slope, band=band, innovation_z=zs,
        q=q, r=r, last_z=float(zs[-1]),
        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
    )


# ---------------------------------------------------------------- attention

@dataclass
class AttentionAnalogs:
    dates: list[str]
    matrix: np.ndarray             # (w, w) row-softmax attention
    top_analogs: list[dict] = field(default_factory=list)
    analog_fwd_mean: float = 0.0   # mean 5d fwd return after top analogs
    fwd_days: int = 5
    window: int = 0
    elapsed_ms: float = 0.0


def attention_analogs(
    ohlc: pd.DataFrame, window: int = 90, top_n: int = 5, fwd_days: int = 5,
    temperature: float = 1.0,
) -> AttentionAnalogs:
    """Untrained self-attention over daily market-state embeddings.

    Each day is embedded as [ret1, ret5, vol10, rsi, volume_z], standardized
    over the window; A = softmax(X·Xᵀ/√d) — which past days the market
    'attends to' from each day. The last row answers: which historical days
    most resemble *today*, and what happened next after them. Kernel/analog
    regression wearing attention's clothes — labeled untrained, no learning.
    """
    t0 = time.perf_counter()
    cols = {c.lower(): c for c in ohlc.columns}
    close = ohlc[cols["close"]].astype(float)
    volume = ohlc[cols["volume"]].astype(float) if "volume" in cols \
        else pd.Series(0.0, index=close.index)
    feats = pd.DataFrame({
        "ret1": close.pct_change(),
        "ret5": close.pct_change(5),
        "vol10": close.pct_change().rolling(10).std(),
        "rsi": (ta.rsi(close, 14) - 50) / 50,
        "volz": (volume - volume.rolling(20).mean()) / volume.rolling(20).std(),
    }).dropna()
    if len(feats) < window + fwd_days + 5:
        raise ValueError(f"Need {window + fwd_days + 5}+ usable days, got {len(feats)}")

    fwd_all = close.pct_change(fwd_days).shift(-fwd_days).reindex(feats.index)
    Xw = feats.iloc[-window:]
    X = Xw.to_numpy()
    X = (X - X.mean(axis=0)) / np.where(X.std(axis=0) < 1e-12, 1.0, X.std(axis=0))
    scores = X @ X.T / (np.sqrt(X.shape[1]) * temperature)
    scores -= scores.max(axis=1, keepdims=True)
    A = np.exp(scores)
    A /= A.sum(axis=1, keepdims=True)

    dates = [str(d)[:10] for d in Xw.index]
    last = A[-1].copy()
    last[-1] = 0.0                                   # today can't be its own analog
    order = np.argsort(last)[::-1][:top_n]
    tops, fwds = [], []
    for i in order:
        f = fwd_all.iloc[-window + i] if (-window + i) < -fwd_days else np.nan
        tops.append({"date": dates[i], "weight": float(last[i]),
                     "fwd": None if pd.isna(f) else float(f)})
        if not pd.isna(f):
            fwds.append(float(f))
    return AttentionAnalogs(
        dates=dates, matrix=A, top_analogs=tops,
        analog_fwd_mean=float(np.mean(fwds)) if fwds else 0.0,
        fwd_days=fwd_days, window=window,
        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
    )
