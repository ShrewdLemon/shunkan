import numpy as np
import pytest

from shunkan.derivatives import (
    analyze_chain,
    bs_greeks,
    bs_price,
    classify_buildup,
    implied_vol,
    norm_cdf,
    synthetic_chain,
)


def test_norm_cdf_known_values():
    assert norm_cdf(0.0) == pytest.approx(0.5, abs=1e-7)
    assert norm_cdf(1.96) == pytest.approx(0.9750, abs=2e-4)
    assert norm_cdf(-1.96) == pytest.approx(0.0250, abs=2e-4)


def test_bs_price_put_call_parity():
    spot, strike, t, sigma, r = 23000.0, 23200.0, 7 / 365, 0.14, 0.065
    call = bs_price(spot, strike, t, sigma, True, r)
    put = bs_price(spot, strike, t, sigma, False, r)
    # C - P = S - K e^{-rt}
    assert float(call - put) == pytest.approx(
        spot - strike * np.exp(-r * t), rel=1e-9
    )


def test_bs_price_intrinsic_bounds():
    # Deep ITM call ~ S - K e^{-rt}; deep OTM call ~ 0
    deep_itm = float(bs_price(23000, 18000, 7 / 365, 0.14, True))
    assert deep_itm == pytest.approx(23000 - 18000 * np.exp(-0.065 * 7 / 365), rel=1e-3)
    assert float(bs_price(23000, 30000, 7 / 365, 0.14, True)) < 1.0


def test_greeks_signs_and_ranges():
    strikes = np.arange(22000.0, 24500.0, 50.0)
    g_call = bs_greeks(23200.0, strikes, 7 / 365, 0.14, True)
    g_put = bs_greeks(23200.0, strikes, 7 / 365, 0.14, False)
    assert ((g_call["delta"] >= 0) & (g_call["delta"] <= 1)).all()
    assert ((g_put["delta"] >= -1) & (g_put["delta"] <= 0)).all()
    assert (g_call["gamma"] > 0).all()
    assert (g_call["vega"] > 0).all()
    assert (g_call["theta"] <= 1e-9).all()  # long options decay


def test_implied_vol_round_trip():
    strikes = np.arange(22500.0, 24000.0, 50.0)
    true_iv = 0.12 + 0.8 * ((strikes - 23200.0) / 23200.0) ** 2
    prices = bs_price(23200.0, strikes, 14 / 365, true_iv, True)
    recovered = implied_vol(prices, 23200.0, strikes, 14 / 365, True)
    assert np.nanmax(np.abs(recovered - true_iv)) < 1e-4


def test_implied_vol_invalid_inputs_are_nan():
    # Price below intrinsic -> NaN, not an exception or garbage.
    iv = implied_vol(np.array([1.0]), 23000.0, np.array([18000.0]), 7 / 365, True)
    assert np.isnan(iv[0])


def test_classify_buildup_quadrants():
    assert classify_buildup(+1, +100) == "long buildup"
    assert classify_buildup(-1, +100) == "short buildup"
    assert classify_buildup(-1, -100) == "long unwinding"
    assert classify_buildup(+1, -100) == "short covering"
    assert classify_buildup(0, 100) == "neutral"


def test_synthetic_chain_is_deterministic():
    a = synthetic_chain("NIFTY")
    b = synthetic_chain("NIFTY")
    assert np.array_equal(a.strikes, b.strikes)
    assert np.array_equal(a.call_oi, b.call_oi)
    assert a.spot == b.spot


def test_analyze_chain_outputs():
    chain = synthetic_chain("NIFTY")
    a = analyze_chain(chain)
    assert a.pcr_oi > 0
    assert a.max_pain in chain.strikes
    assert a.support in chain.strikes
    assert a.resistance in chain.strikes
    assert 0.0 < a.atm_iv < 1.0
    assert a.expected_move_pct > 0
    assert a.bias in ("bullish", "bearish", "neutral")
    # The synthetic generator plants unusual-activity strikes deliberately.
    assert len(a.unusual) >= 1


def test_chain_iv_solver_fills_missing():
    chain = synthetic_chain("BANKNIFTY")
    chain.call_iv = np.full_like(chain.call_iv, np.nan)
    chain.put_iv = np.full_like(chain.put_iv, np.nan)
    chain.ensure_iv()
    # ATM region must recover (deep wings may legitimately fail intrinsic checks)
    i = chain.atm_index
    assert not np.isnan(chain.call_iv[i])
    assert not np.isnan(chain.put_iv[i])
