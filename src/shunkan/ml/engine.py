"""ML studio engine — direction models in pure numpy.

No sklearn, no model zoo theater: two models that fit the terminal's rules
(vectorized, explainable, honest about limits):

- ridge      — closed-form ridge regression on standardized features,
               predicting the forward return; direction = sign.
- stumps     — gradient-boosted decision stumps on the logistic loss,
               with per-feature gain importances. ~50 lines of numpy.

Splits are strictly chronological (train on the past, test on the future),
metrics always ship next to the majority-class baseline, and training
refuses to run on fewer than MIN_ROWS observations rather than reporting
noise as skill.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from shunkan.analytics import indicators as ta

MIN_ROWS = 200

# feature name -> (builder(ohlc_lowercase_frame) -> Series, description)
FEATURES: dict[str, tuple] = {}


def _feature(name, desc):
    def deco(fn):
        FEATURES[name] = (fn, desc)
        return fn
    return deco


@_feature("ret1", "1-day return")
def _f_ret1(df):
    return df["close"].pct_change()


@_feature("ret5", "5-day return")
def _f_ret5(df):
    return df["close"].pct_change(5)


@_feature("rsi14", "RSI(14) scaled to [-1,1]")
def _f_rsi(df):
    return (ta.rsi(df["close"], 14) - 50.0) / 50.0


@_feature("ema_gap", "EMA12/EMA26 gap")
def _f_ema_gap(df):
    return ta.ema(df["close"], 12) / ta.ema(df["close"], 26) - 1.0


@_feature("vol20", "20-day return volatility")
def _f_vol20(df):
    return df["close"].pct_change().rolling(20).std()


@_feature("zscore20", "close z-score vs 20-day mean")
def _f_z(df):
    m = df["close"].rolling(20).mean()
    s = df["close"].rolling(20).std()
    return (df["close"] - m) / s


@_feature("range_pct", "daily high-low range / close")
def _f_range(df):
    return (df["high"] - df["low"]) / df["close"]


@_feature("volume_z", "volume z-score vs 20-day")
def _f_volz(df):
    m = df["volume"].rolling(20).mean()
    s = df["volume"].rolling(20).std()
    return (df["volume"] - m) / s


@dataclass
class MLResult:
    model: str
    features: list[str]
    horizon: int
    n_train: int
    n_test: int
    acc_train: float
    acc_test: float
    baseline_test: float          # majority-class accuracy on test
    up_ret_test: float            # mean fwd return when model says up
    down_ret_test: float          # mean fwd return when model says down
    importances: dict[str, float] = field(default_factory=dict)
    equity_model: np.ndarray = field(repr=False, default=None)   # test segment
    equity_bh: np.ndarray = field(repr=False, default=None)
    test_index: list = field(repr=False, default_factory=list)
    elapsed_ms: float = 0.0


def _matrix(ohlc: pd.DataFrame, names: list[str], horizon: int):
    cols = {c.lower(): c for c in ohlc.columns}
    df = pd.DataFrame({k: ohlc[cols[k]].astype(float)
                       for k in ("open", "high", "low", "close", "volume")
                       if k in cols})
    if "volume" not in df:
        df["volume"] = 0.0
    X = pd.DataFrame({n: FEATURES[n][0](df) for n in names})
    fwd = df["close"].pct_change(horizon).shift(-horizon)
    data = pd.concat([X, fwd.rename("_fwd")], axis=1).dropna()
    return data[names].to_numpy(), data["_fwd"].to_numpy(), list(data.index)


def _standardize(X, mu=None, sd=None):
    mu = X.mean(axis=0) if mu is None else mu
    sd = np.where(X.std(axis=0) < 1e-12, 1.0, X.std(axis=0)) if sd is None else sd
    return (X - mu) / sd, mu, sd


def _fit_ridge(X, y, lam=1.0):
    n = X.shape[1]
    A = X.T @ X + lam * np.eye(n)
    return np.linalg.solve(A, X.T @ y)


def _fit_stumps(X, y_sign, n_rounds=60, lr=0.15, n_cuts=16):
    """Gradient boosting with depth-1 trees on the logistic loss."""
    n, d = X.shape
    F = np.zeros(n)
    stumps, gains = [], np.zeros(d)
    cuts = [np.quantile(X[:, j], np.linspace(0.08, 0.92, n_cuts)) for j in range(d)]
    for _ in range(n_rounds):
        g = y_sign / (1.0 + np.exp(y_sign * F))       # -dL/dF
        best = None
        for j in range(d):
            xj = X[:, j]
            for t in cuts[j]:
                m = xj <= t
                nl, nr = m.sum(), n - m.sum()
                if nl < 8 or nr < 8:
                    continue
                gl, gr = g[m].mean(), g[~m].mean()
                score = nl * gl * gl + nr * gr * gr
                if best is None or score > best[0]:
                    best = (score, j, t, gl, gr)
        if best is None:
            break
        _, j, t, gl, gr = best
        F += lr * np.where(X[:, j] <= t, gl, gr)
        gains[j] += best[0]
        stumps.append((j, t, lr * gl, lr * gr))

    def predict(Xn):
        out = np.zeros(len(Xn))
        for j, t, vl, vr in stumps:
            out += np.where(Xn[:, j] <= t, vl, vr)
        return out

    return predict, gains


def train_model(
    ohlc: pd.DataFrame, features: list[str], model: str = "stumps",
    horizon: int = 5, test_split: float = 0.25,
) -> MLResult:
    t0 = time.perf_counter()
    names = [f for f in features if f in FEATURES]
    if len(names) < 2:
        raise ValueError(f"Pick >= 2 known features from: {', '.join(FEATURES)}")
    if model not in ("ridge", "stumps"):
        raise ValueError("model must be 'ridge' or 'stumps'")

    X, fwd, idx = _matrix(ohlc, names, horizon)
    if len(X) < MIN_ROWS:
        raise ValueError(f"Need {MIN_ROWS}+ usable rows after features/horizon, "
                         f"got {len(X)} — not enough to say anything honest")

    cut = int(len(X) * (1.0 - test_split))
    Xtr, Xte = X[:cut], X[cut:]
    ytr, yte = fwd[:cut], fwd[cut:]
    str_, mu, sd = _standardize(Xtr)
    ste = _standardize(Xte, mu, sd)[0]
    sign_tr, sign_te = np.sign(ytr), np.sign(yte)

    if model == "ridge":
        w = _fit_ridge(str_, ytr)
        pred_tr, pred_te = str_ @ w, ste @ w
        importances = {n: abs(float(wi)) for n, wi in zip(names, w)}
    else:
        predict, gains = _fit_stumps(str_, np.where(sign_tr >= 0, 1.0, -1.0))
        pred_tr, pred_te = predict(str_), predict(ste)
        importances = {n: float(g) for n, g in zip(names, gains)}
    tot = sum(importances.values()) or 1.0
    importances = {k: v / tot for k, v in
                   sorted(importances.items(), key=lambda kv: -kv[1])}

    dir_tr, dir_te = np.sign(pred_tr), np.sign(pred_te)
    acc = lambda p, y: float((p == np.sign(y))[y != 0].mean())
    up_mask, down_mask = dir_te > 0, dir_te < 0

    # equity if you traded the test-segment calls: hold each direction for
    # `horizon` bars via the forward return (frictionless — labeled in UI)
    step = max(horizon, 1)
    picks = np.arange(0, len(yte) - 1, step)
    eq_model = np.cumprod(1.0 + dir_te[picks] * yte[picks])
    eq_bh = np.cumprod(1.0 + yte[picks])

    return MLResult(
        model=model, features=names, horizon=horizon,
        n_train=cut, n_test=len(X) - cut,
        acc_train=acc(dir_tr, ytr), acc_test=acc(dir_te, yte),
        baseline_test=float(max((sign_te > 0).mean(), (sign_te <= 0).mean())),
        up_ret_test=float(yte[up_mask].mean()) if up_mask.any() else 0.0,
        down_ret_test=float(yte[down_mask].mean()) if down_mask.any() else 0.0,
        importances=importances,
        equity_model=eq_model, equity_bh=eq_bh,
        test_index=[str(idx[cut + i])[:10] for i in picks],
        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
    )
