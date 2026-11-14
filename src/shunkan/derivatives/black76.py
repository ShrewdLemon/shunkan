"""Black-76 on the forward: price, greeks and implied vol, vectorised, accurate in the wings.

Prices are in points per unit of the underlying. F is the forward of the option's
expiry, DF the discount factor to that expiry, T years. delta is N(d1) for calls and
N(d1) - 1 for puts (the delta traders quote, and NSE's chain shows with no dividend);
gamma is per point of forward, vega per vol point, theta per calendar day.

The normal CDF here has relative error under 1.2e-7 in both tails (Numerical
Recipes erfcc), so a 0.05-point wing is priced as well as an ATM strike;
greeks.norm_cdf is absolute-accurate only and stays as it is for the lab's
registered deflated Sharpes.
"""

from __future__ import annotations

import numpy as np

OK, EXPIRED, BELOW_INTRINSIC, ABOVE_MAX, NO_FORWARD, NOT_CONVERGED = 0, 1, 2, 3, 4, 5
CODES = {OK: "ok", EXPIRED: "expired", BELOW_INTRINSIC: "below intrinsic",
         ABOVE_MAX: "above max", NO_FORWARD: "no forward", NOT_CONVERGED: "not converged"}
VOL_LO, VOL_HI = 1e-4, 5.0
_C = (-1.26551223, 1.00002368, 0.37409196, 0.09678418, -0.18628806,
      0.27886807, -1.13520398, 1.48851587, -0.82215223, 0.17087277)
_INV_SQRT_2PI = 1.0 / np.sqrt(2.0 * np.pi)


def _erfc(x: np.ndarray) -> np.ndarray:
    z = np.abs(x)
    t = 1.0 / (1.0 + 0.5 * z)
    poly = _C[-1]
    for c in _C[-2::-1]:
        poly = c + t * poly
    ans = t * np.exp(-z * z + poly)
    return np.where(x >= 0, ans, 2.0 - ans)


def ncdf(x) -> np.ndarray:
    return 0.5 * _erfc(-np.asarray(x, dtype=np.float64) / np.sqrt(2.0))


def npdf(x) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    return _INV_SQRT_2PI * np.exp(-0.5 * x * x)


def _arrays(*a):
    a = [np.asarray(v, dtype=np.float64) for v in a]
    shape = np.broadcast_shapes(*(v.shape for v in a))
    return [np.broadcast_to(v, shape) for v in a], shape


def _d(F, K, T, sigma):
    sd = sigma * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sd * sd) / sd
    return d1, d1 - sd


def price(F, K, T, DF, sigma, is_call) -> np.ndarray:
    (F, K, T, DF, sigma), shape = _arrays(F, K, T, DF, sigma)
    call = np.broadcast_to(np.asarray(is_call, dtype=bool), shape)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1, d2 = _d(F, K, T, sigma)
        c = DF * (F * ncdf(d1) - K * ncdf(d2))
        p = DF * (K * ncdf(-d2) - F * ncdf(-d1))
    return np.where(call, c, p)


def greeks(F, K, T, DF, sigma, is_call) -> dict[str, np.ndarray]:
    (F, K, T, DF, sigma), shape = _arrays(F, K, T, DF, sigma)
    call = np.broadcast_to(np.asarray(is_call, dtype=bool), shape)
    with np.errstate(divide="ignore", invalid="ignore"):
        sqrt_t = np.sqrt(T)
        d1, d2 = _d(F, K, T, sigma)
        n1 = npdf(d1)
        nd1 = ncdf(d1)
        value = np.where(call, DF * (F * nd1 - K * ncdf(d2)),
                         DF * (K * ncdf(-d2) - F * ncdf(-d1)))
        r = -np.log(DF) / T
        return {
            "delta": np.where(call, nd1, nd1 - 1.0),
            "gamma": n1 / (F * sigma * sqrt_t),
            "vega": DF * F * n1 * sqrt_t / 100.0,
            "theta": (r * value - DF * F * n1 * sigma / (2.0 * sqrt_t)) / 365.0,
        }


def implied_vol(px, F, K, T, DF, is_call, iters: int = 60) -> tuple[np.ndarray, np.ndarray]:
    """(iv, code) per entry: safeguarded Newton inside a shrinking bracket.

    code is one of CODES; iv is NaN unless code is OK. A price at or below
    discounted intrinsic, or at or above the no-arbitrage cap (DF*F for a call,
    DF*K for a put) or the price at 500% vol, has no IV. An in-the-money price is
    solved as its out-of-the-money twin by parity (same IV in Black-76), so the
    solver works on time value, not on intrinsic.
    """
    (px, F, K, T, DF), shape = _arrays(px, F, K, T, DF)
    call = np.broadcast_to(np.asarray(is_call, dtype=bool), shape)
    code = np.full(shape, OK, dtype=np.int8)
    finite = np.isfinite(F) & np.isfinite(DF) & (F > 0) & (DF > 0)
    code[~finite] = NO_FORWARD
    code[finite & ~(T > 0)] = EXPIRED
    with np.errstate(invalid="ignore"):
        intrinsic = DF * np.where(call, np.maximum(F - K, 0.0), np.maximum(K - F, 0.0))
        cap = DF * np.where(call, F, K)
        live = code == OK
        code[live & ~(px > intrinsic)] = BELOW_INTRINSIC
        live = code == OK
        code[live & ~(px < cap)] = ABOVE_MAX
    live = code == OK
    idx = np.flatnonzero(live)
    iv = np.full(shape, np.nan)
    if idx.size:
        p, f, k, t, df, c = (a.ravel()[idx] for a in (px, F, K, T, DF, call))
        itm = np.where(c, k < f, k > f)
        p = np.where(itm, p - df * np.abs(f - k), p)
        c = np.where(itm, ~c, c)
        top = price(f, k, t, df, VOL_HI, c)
        over = ~(p < top)
        lo = np.full(idx.size, VOL_LO)
        hi = np.full(idx.size, VOL_HI)
        x = np.sqrt(2.0 * np.abs(np.log(f / k)) / t)
        sigma = np.clip(np.where(x > 0.05, x, 0.2), 0.01, 3.0)
        tol = 1e-10 * np.maximum(p, 1e-4)
        done = over.copy()
        for _ in range(iters):
            if done.all():
                break
            v = price(f, k, t, df, sigma, c)
            err = v - p
            hit = np.abs(err) <= tol
            done |= hit
            hi = np.where(err > 0, sigma, hi)
            lo = np.where(err < 0, sigma, lo)
            sd = np.sqrt(t)
            d1 = (np.log(f / k) + 0.5 * sigma * sigma * t) / (sigma * sd)
            vega = df * f * npdf(d1) * sd
            with np.errstate(divide="ignore", invalid="ignore"):
                step = sigma - err / vega
            bad = ~np.isfinite(step) | (step <= lo) | (step >= hi)
            sigma = np.where(done, sigma, np.where(bad, 0.5 * (lo + hi), step))
        v = price(f, k, t, df, sigma, c)
        conv = (np.abs(v - p) <= 1e-7 * np.maximum(p, 1e-2)) & ~over
        out_code = np.where(over, ABOVE_MAX, np.where(conv, OK, NOT_CONVERGED)).astype(np.int8)
        flat_iv = iv.reshape(-1)
        flat_code = code.reshape(-1)
        flat_iv[idx] = np.where(out_code == OK, sigma, np.nan)
        flat_code[idx] = out_code
    return iv, code
