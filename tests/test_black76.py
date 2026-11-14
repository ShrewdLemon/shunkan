import math

import numpy as np
import pytest

from shunkan.derivatives import black76 as b


def test_cdf_is_relatively_accurate_in_both_tails():
    x = np.linspace(-12, 12, 4801)
    ref = np.array([0.5 * math.erfc(-v / math.sqrt(2)) for v in x])
    assert np.max(np.abs(b.ncdf(x) - ref) / ref) < 2e-7


def grid():
    F = np.array([500.0, 25000.0, 60000.0])
    m = np.array([-0.3, -0.05, 0.0, 0.04, 0.25])
    T = np.array([2 / 365, 30 / 365, 1.0, 4.0])
    s = np.array([0.06, 0.15, 0.6])
    F, m, T, s, c = np.meshgrid(F, m, T, s, [True, False], indexing="ij")
    F, m, T, s, c = (a.ravel() for a in (F, m, T, s, c))
    K = F * np.exp(m)
    DF = np.exp(-0.065 * T)
    return F, K, T, DF, s, c


def test_iv_recovers_the_vol_that_made_the_price():
    F, K, T, DF, s, c = grid()
    px = b.price(F, K, T, DF, s, c)
    tv = px - DF * np.where(c, np.maximum(F - K, 0), np.maximum(K - F, 0))
    keep = tv > 1e-6 * F
    iv, code = b.implied_vol(px[keep], F[keep], K[keep], T[keep], DF[keep], c[keep])
    assert (code == b.OK).all()
    assert np.max(np.abs(iv - s[keep])) < 1e-8


def test_itm_call_and_otm_put_at_one_strike_give_one_iv():
    F, K, T, DF, s = 25000.0, 24000.0, 20 / 365, math.exp(-0.065 * 20 / 365), 0.18
    call = b.price(F, K, T, DF, s, True)
    put = b.price(F, K, T, DF, s, False)
    iv, code = b.implied_vol([call, put], F, K, T, DF, [True, False])
    assert list(code) == [b.OK, b.OK]
    assert iv == pytest.approx([s, s], abs=1e-10)


def test_codes_say_why_there_is_no_iv():
    T, DF = 10 / 365, math.exp(-0.065 * 10 / 365)
    px = [DF * 1000 - 0.5,        # call below discounted intrinsic
          DF * 25000,             # call at the cap DF*F
          150.0,                  # expired
          150.0,                  # no forward
          DF * 24999.0]           # far above the price at 500% vol for a 10-day put
    F = [25000.0, 25000.0, 25000.0, np.nan, 25000.0]
    K = [24000.0, 25000.0, 25000.0, 25000.0, 26000.0]
    t = [T, T, 0.0, T, T]
    iv, code = b.implied_vol(px, F, K, t, DF, [True, True, True, True, False])
    assert list(code) == [b.BELOW_INTRINSIC, b.ABOVE_MAX, b.EXPIRED, b.NO_FORWARD, b.ABOVE_MAX]
    assert np.isnan(iv).all()


@pytest.mark.parametrize("call", [True, False])
@pytest.mark.parametrize("K", [23500.0, 25000.0, 26500.0])
def test_greeks_match_finite_differences(call, K):
    F, T, r, s = 25000.0, 45 / 365, 0.065, 0.16
    DF = math.exp(-r * T)
    g = {k: float(v) for k, v in b.greeks(F, K, T, DF, s, call).items()}

    def v(f=F, t=T, sig=s):
        return float(b.price(f, K, t, math.exp(-r * t), sig, call))

    h = 0.5
    assert g["delta"] == pytest.approx((v(F + h) - v(F - h)) / (2 * h) / DF, rel=1e-5, abs=1e-7)
    up, dn = (float(b.greeks(F + d, K, T, DF, s, call)["delta"]) for d in (5.0, -5.0))
    assert g["gamma"] == pytest.approx((up - dn) / 10.0, rel=1e-4)
    assert g["vega"] == pytest.approx((v(sig=s + 1e-4) - v(sig=s - 1e-4)) / 2e-4 / 100, rel=1e-5)
    day = 1 / 365
    assert g["theta"] == pytest.approx(-(v(t=T + day * 1e-3) - v(t=T - day * 1e-3)) / 2e-3, rel=1e-4)


def test_atm_numbers_are_the_familiar_ones():
    T = 30 / 365
    g = b.greeks(25000.0, 25000.0, T, math.exp(-0.06 * T), 0.13, True)
    assert float(g["delta"]) == pytest.approx(0.507, abs=1e-3)
    assert float(g["vega"]) == pytest.approx(28.4, abs=0.1)
    assert float(g["theta"]) == pytest.approx(-6.10, abs=0.01)


def test_200k_rows_solve_in_under_two_seconds():
    rng = np.random.default_rng(1)
    n = 200_000
    F = rng.uniform(1000, 60000, n)
    K = F * np.exp(rng.uniform(-0.3, 0.3, n))
    T = rng.uniform(2 / 365, 2, n)
    DF = np.exp(-0.065 * T)
    s = rng.uniform(0.08, 0.9, n)
    c = rng.random(n) < 0.5
    px = b.price(F, K, T, DF, s, c)
    import time
    t0 = time.perf_counter()
    b.implied_vol(px, F, K, T, DF, c)
    assert time.perf_counter() - t0 < 2.0
