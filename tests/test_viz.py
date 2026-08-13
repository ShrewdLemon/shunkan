"""3D visualization grids: IV surface, greeks surfaces, Monte Carlo fans."""

import numpy as np
import pytest

from shunkan.analytics.viz import greeks_surface, iv_surface, price_fan
from shunkan.data.chains import get_chain
from shunkan.derivatives.greeks import bs_greeks


@pytest.fixture(scope="module")
def chain():
    return get_chain("NIFTY")


@pytest.fixture(scope="module")
def surface(chain):
    return iv_surface(chain)


def test_iv_surface_shape_and_sanity(surface):
    n_t, n_k = surface.iv.shape
    assert n_t == len(surface.days) and n_k == len(surface.strikes)
    assert np.isfinite(surface.iv).all()
    assert (surface.iv > 0.005).all() and (surface.iv < 3.0).all()


def test_iv_surface_market_row_is_the_chain_smile(surface, chain):
    """The row at the chain's expiry must reproduce the actual smile (damp=1)."""
    row = surface.iv[surface.market_row]
    smile = np.nanmean(np.vstack([chain.call_iv, chain.put_iv]), axis=0)
    smile = smile[~np.isnan(smile)]
    damp = np.sqrt(surface.chain_days / surface.days[surface.market_row])
    expected = surface.atm_iv + (smile - surface.atm_iv) * damp
    np.testing.assert_allclose(row, expected, rtol=1e-9)


def test_iv_surface_smile_flattens_with_maturity(surface):
    """Wing deviation from ATM must shrink as maturity grows."""
    dev = np.abs(surface.iv - surface.atm_iv)
    wing = dev[:, 0]  # deepest wing strike across maturities
    if wing.max() > 1e-6:  # only meaningful when the smile has wings
        assert wing[np.argmax(surface.days)] <= wing[np.argmin(surface.days)] + 1e-9


def test_greeks_surface_matches_pointwise_bs():
    g = greeks_surface(100.0, 0.2, greek="gamma", n_strikes=11, n_days=7)
    assert g.values.shape == (7, 11)
    k, d = g.strikes[3], g.days[5]
    direct = bs_greeks(100.0, k, d / 365.0, 0.2, True)["gamma"]
    assert abs(g.values[5, 3] - float(direct)) < 1e-12


def test_greeks_surface_rejects_unknown_greek():
    with pytest.raises(ValueError, match="Unknown greek"):
        greeks_surface(100.0, 0.2, greek="charm")


def test_gamma_peaks_at_the_money():
    g = greeks_surface(100.0, 0.2, greek="gamma", n_strikes=41, n_days=5)
    nearest_expiry = g.values[0]
    peak_strike = g.strikes[np.argmax(nearest_expiry)]
    assert abs(peak_strike - 100.0) < 3.0


@pytest.fixture(scope="module")
def fan(provider_module):
    hist = provider_module.history("TEST", period="2y")
    spot = float(hist["close" if "close" in hist.columns else "Close"].iloc[-1])
    return price_fan(hist, spot, symbol="TEST", horizon_days=40,
                     n_paths=500, n_display=20)


@pytest.fixture(scope="module")
def provider_module():
    from shunkan.data.provider import SyntheticProvider

    return SyntheticProvider()


def test_fan_shapes(fan):
    assert fan.display_paths.shape == (20, 40)
    assert len(fan.days) == 40
    assert len(fan.terminal_bins) == len(fan.terminal_freq) == 31


def test_fan_envelope_ordering(fan):
    e = fan.envelope
    assert (e["p5"] <= e["p25"]).all()
    assert (e["p25"] <= e["p50"]).all()
    assert (e["p50"] <= e["p75"]).all()
    assert (e["p75"] <= e["p95"]).all()


def test_fan_paths_start_near_spot(fan):
    """Day-1 levels are spot times one daily return — tight around spot."""
    day1 = fan.display_paths[:, 0]
    assert (np.abs(day1 / fan.spot - 1.0) < 0.25).all()


def test_fan_deterministic(provider_module):
    hist = provider_module.history("TEST", period="2y")
    a = price_fan(hist, 100.0, horizon_days=30, n_paths=300)
    b = price_fan(hist, 100.0, horizon_days=30, n_paths=300)
    np.testing.assert_array_equal(a.display_paths, b.display_paths)
    assert a.prob_up == b.prob_up


def test_fan_refuses_thin_history(provider_module):
    hist = provider_module.history("TEST", period="2y").head(20)
    with pytest.raises(ValueError, match="Need at least"):
        price_fan(hist, 100.0)
