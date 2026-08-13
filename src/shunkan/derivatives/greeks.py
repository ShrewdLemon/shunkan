"""Vectorized Black-Scholes pricing, greeks, and implied volatility.

Pure numpy — no scipy, no per-strike Python loops. A 200-strike chain's
full greeks + IV solve runs in well under a millisecond, so the option
chain panel can recompute on every refresh tick.

Conventions: t in years, rates continuously compounded, q = dividend yield.
`is_call` may be a scalar or a boolean array broadcast against strikes.
"""

from __future__ import annotations

import numpy as np

SQRT_2 = np.sqrt(2.0)
INV_SQRT_2PI = 1.0 / np.sqrt(2.0 * np.pi)

# Abramowitz & Stegun 7.1.26 rational approximation of erf.
# Max abs error 1.5e-7 — far below display/decision precision for IV work,
# and ~40x faster than calling math.erf in a Python loop over a chain.
_A1, _A2, _A3, _A4, _A5 = (
    0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429,
)
_P = 0.3275911


def _erf(x: np.ndarray) -> np.ndarray:
    sign = np.sign(x)
    ax = np.abs(x)
    t = 1.0 / (1.0 + _P * ax)
    poly = t * (_A1 + t * (_A2 + t * (_A3 + t * (_A4 + t * _A5))))
    return sign * (1.0 - poly * np.exp(-ax * ax))


def norm_cdf(x: np.ndarray | float) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    return 0.5 * (1.0 + _erf(x / SQRT_2))


def norm_pdf(x: np.ndarray | float) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    return INV_SQRT_2PI * np.exp(-0.5 * x * x)


def _d1_d2(spot, strike, t, sigma, r, q):
    sigma_sqrt_t = sigma * np.sqrt(t)
    # Guard log-moneyness against non-positive inputs (bad feed data) —
    # produces a deep-ITM/OTM d1 instead of NaN contagion across the chain.
    ratio = np.maximum(spot, 1e-12) / np.maximum(strike, 1e-12)
    d1 = (np.log(ratio) + (r - q + 0.5 * sigma**2) * t) / sigma_sqrt_t
    return d1, d1 - sigma_sqrt_t


def bs_price(
    spot, strike, t, sigma, is_call=True, r: float = 0.065, q: float = 0.0
) -> np.ndarray:
    """Black-Scholes option price (default r = 6.5%, Indian risk-free ballpark)."""
    spot, strike, t, sigma = map(
        lambda a: np.asarray(a, dtype=np.float64), (spot, strike, t, sigma)
    )
    t = np.maximum(t, 1e-9)
    sigma = np.maximum(sigma, 1e-9)
    d1, d2 = _d1_d2(spot, strike, t, sigma, r, q)
    call = spot * np.exp(-q * t) * norm_cdf(d1) - strike * np.exp(-r * t) * norm_cdf(d2)
    put = strike * np.exp(-r * t) * norm_cdf(-d2) - spot * np.exp(-q * t) * norm_cdf(-d1)
    return np.where(np.asarray(is_call, dtype=bool), call, put)


def bs_greeks(
    spot, strike, t, sigma, is_call=True, r: float = 0.065, q: float = 0.0
) -> dict[str, np.ndarray]:
    """Delta, gamma, theta (per calendar day), vega (per vol point), rho.

    Returns arrays broadcast to the common shape of the inputs.
    """
    spot, strike, t, sigma = map(
        lambda a: np.asarray(a, dtype=np.float64), (spot, strike, t, sigma)
    )
    t = np.maximum(t, 1e-9)
    sigma = np.maximum(sigma, 1e-9)
    is_call = np.asarray(is_call, dtype=bool)

    d1, d2 = _d1_d2(spot, strike, t, sigma, r, q)
    pdf_d1 = norm_pdf(d1)
    disc_r = np.exp(-r * t)
    disc_q = np.exp(-q * t)
    sqrt_t = np.sqrt(t)

    delta_call = disc_q * norm_cdf(d1)
    delta = np.where(is_call, delta_call, delta_call - disc_q)
    gamma = disc_q * pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * disc_q * pdf_d1 * sqrt_t / 100.0  # per 1 vol-point

    theta_common = -spot * disc_q * pdf_d1 * sigma / (2.0 * sqrt_t)
    theta_call = theta_common - r * strike * disc_r * norm_cdf(d2) + q * spot * disc_q * norm_cdf(d1)
    theta_put = theta_common + r * strike * disc_r * norm_cdf(-d2) - q * spot * disc_q * norm_cdf(-d1)
    theta = np.where(is_call, theta_call, theta_put) / 365.0  # per calendar day

    rho_call = strike * t * disc_r * norm_cdf(d2) / 100.0
    rho_put = -strike * t * disc_r * norm_cdf(-d2) / 100.0
    rho = np.where(is_call, rho_call, rho_put)

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho}


def implied_vol(
    price, spot, strike, t, is_call=True, r: float = 0.065, q: float = 0.0,
    max_iter: int = 24, tol: float = 1e-6,
) -> np.ndarray:
    """Vectorized implied volatility: Newton-Raphson with bisection fallback.

    Entries with no valid IV (price below intrinsic, zero time, zero price)
    come back as NaN rather than raising.
    """
    price, spot, strike, t = map(
        lambda a: np.asarray(a, dtype=np.float64), (price, spot, strike, t)
    )
    is_call_arr = np.broadcast_to(np.asarray(is_call, dtype=bool), np.broadcast_shapes(
        price.shape, spot.shape, strike.shape, t.shape
    )).copy() if np.ndim(is_call) or True else is_call

    shape = np.broadcast_shapes(price.shape, spot.shape, strike.shape, t.shape)
    price, spot, strike, t = (np.broadcast_to(a, shape).astype(np.float64)
                              for a in (price, spot, strike, t))
    is_call_arr = np.broadcast_to(np.asarray(is_call, dtype=bool), shape)

    intrinsic = np.where(
        is_call_arr,
        np.maximum(spot * np.exp(-q * np.maximum(t, 1e-9)) - strike * np.exp(-r * np.maximum(t, 1e-9)), 0.0),
        np.maximum(strike * np.exp(-r * np.maximum(t, 1e-9)) - spot * np.exp(-q * np.maximum(t, 1e-9)), 0.0),
    )
    valid = (price > intrinsic + 1e-10) & (t > 1e-9) & (price > 0) & (spot > 0)

    # Brenner-Subrahmanyam seed, clamped to a sane band.
    sigma = np.clip(
        np.sqrt(2.0 * np.pi / np.maximum(t, 1e-9)) * price / np.maximum(spot, 1e-9),
        0.01, 3.0,
    )

    for _ in range(max_iter):
        theo = bs_price(spot, strike, t, sigma, is_call_arr, r, q)
        d1, _ = _d1_d2(spot, strike, np.maximum(t, 1e-9), np.maximum(sigma, 1e-9), r, q)
        vega_raw = spot * np.exp(-q * t) * norm_pdf(d1) * np.sqrt(np.maximum(t, 1e-9))
        step = np.where(vega_raw > 1e-12, (theo - price) / np.maximum(vega_raw, 1e-12), 0.0)
        sigma = np.clip(sigma - np.clip(step, -0.5, 0.5), 1e-4, 5.0)

    # Bisection clean-up for any stragglers Newton didn't converge.
    theo = bs_price(spot, strike, t, sigma, is_call_arr, r, q)
    stubborn = valid & (np.abs(theo - price) > tol * np.maximum(price, 1.0))
    if stubborn.any():
        lo = np.full(shape, 1e-4)
        hi = np.full(shape, 5.0)
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            mid_price = bs_price(spot, strike, t, mid, is_call_arr, r, q)
            too_high = mid_price > price
            hi = np.where(stubborn & too_high, mid, hi)
            lo = np.where(stubborn & ~too_high, mid, lo)
        sigma = np.where(stubborn, 0.5 * (lo + hi), sigma)

    return np.where(valid, sigma, np.nan)
