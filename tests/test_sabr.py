"""SABR calibration: does it recover truth, and does it refuse when it can't."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from shunkan.server import create_app

from shunkan.derivatives.sabr import (
    MIN_STRIKES,
    calibrate_chain,
    calibrate_sabr,
    sabr_iv,
)

F = 24_500.0
T = 7.0 / 365.0


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app()) as c:
        yield c


def smile(alpha=0.30 * F ** 0.5, beta=0.5, rho=-0.45, nu=0.85, n=21, width=0.10):
    k = np.linspace(F * (1 - width), F * (1 + width), n)
    return k, sabr_iv(F, k, T, alpha, beta, rho, nu)


# -- the formula --------------------------------------------------------------


def test_atm_is_finite_and_smooth_through_the_forward():
    """z/x(z) is 0/0 at the money. A strike a rupee away is just as bad, so
    the expansion has to cover a neighbourhood, not an exact equality."""
    k = np.array([F - 1.0, F - 1e-9, F, F + 1e-9, F + 1.0])
    iv = sabr_iv(F, k, T, 0.30 * F ** 0.5, 0.5, -0.45, 0.85)
    assert np.all(np.isfinite(iv))
    assert np.all(iv > 0)
    assert np.max(np.abs(np.diff(iv))) < 5e-4  # no discontinuity at the seam


def test_negative_rho_tilts_the_smile_down_in_strike():
    """Index smiles slope down: downside strikes carry more vol. If this
    inverts, the sign convention has drifted."""
    k = np.array([F * 0.92, F, F * 1.08])
    iv = sabr_iv(F, k, T, 0.30 * F ** 0.5, 0.5, -0.6, 0.9)
    assert iv[0] > iv[1] > iv[2]


def test_more_vol_of_vol_means_more_curvature():
    k = np.array([F * 0.92, F, F * 1.08])
    flat = sabr_iv(F, k, T, 0.30 * F ** 0.5, 0.5, 0.0, 0.10)
    curved = sabr_iv(F, k, T, 0.30 * F ** 0.5, 0.5, 0.0, 1.50)
    wing_lift = lambda v: (v[0] + v[2]) / 2 - v[1]  # noqa: E731
    assert wing_lift(curved) > wing_lift(flat)


# -- calibration --------------------------------------------------------------


def test_recovers_the_parameters_it_was_generated_from():
    k, iv = smile(rho=-0.45, nu=0.85)
    fit = calibrate_sabr(F, k, iv, T, beta=0.5)
    assert fit.rho == pytest.approx(-0.45, abs=0.02)
    assert fit.nu == pytest.approx(0.85, abs=0.05)
    assert fit.rmse_vol_points < 0.01
    assert fit.quality == "tight"


def test_the_fit_can_price_a_strike_nobody_quoted():
    """The whole point of a surface: interpolate where there is no market."""
    k, iv = smile()
    fit = calibrate_sabr(F, k, iv, T, beta=0.5)
    gap = F * 1.035
    assert fit.iv(gap) == pytest.approx(
        sabr_iv(F, gap, T, 0.30 * F ** 0.5, 0.5, -0.45, 0.85), rel=1e-3)


def test_a_bad_fit_reports_itself_instead_of_smoothing():
    """Noise is not a smile. The residuals must say so rather than the fit
    quietly returning whatever minimised least squares."""
    k, iv = smile(n=15)
    rng = np.random.default_rng(0)
    iv = iv + rng.normal(0, 0.03, size=iv.shape)  # 3 vol points of noise
    fit = calibrate_sabr(F, k, iv, T, beta=0.5)
    assert fit.quality == "poor"
    assert fit.good is False
    assert fit.rmse_vol_points > 0.5


def test_refuses_a_smile_too_thin_to_fit():
    k, iv = smile(n=MIN_STRIKES - 1)
    with pytest.raises(ValueError, match="too thin"):
        calibrate_sabr(F, k, iv, T)


def test_unquoted_strikes_are_dropped_not_zero_filled():
    k, iv = smile(n=21)
    iv[:5] = np.nan          # illiquid wing, no quote
    fit = calibrate_sabr(F, k, iv, T, beta=0.5)
    assert fit.n_used == 16
    assert fit.n_available == 21
    assert np.all(np.isfinite(fit.market_iv))


def test_deep_wings_are_excluded_from_the_fit():
    """A one-tick move on a far wing swings its implied vol by whole points
    and would drag the whole surface with it."""
    k = np.concatenate([np.array([F * 0.4, F * 1.9]), smile()[0]])
    iv = sabr_iv(F, k, T, 0.30 * F ** 0.5, 0.5, -0.45, 0.85)
    fit = calibrate_sabr(F, k, iv, T, beta=0.5)
    assert fit.n_used == len(k) - 2
    assert fit.strikes.min() > F * 0.5


# -- the honesty rule ---------------------------------------------------------


def test_refuses_to_calibrate_a_modelled_chain():
    """Fitting a model to synthetic quotes recovers the generator's own
    parameters and would render them as observed market structure."""
    from shunkan.derivatives.synthetic import synthetic_chain

    c = synthetic_chain("NIFTY")
    assert c.is_model is True
    with pytest.raises(ValueError, match="modelled chain"):
        calibrate_chain(c)


def test_calibrates_a_chain_marked_real():
    from shunkan.derivatives.synthetic import synthetic_chain

    c = synthetic_chain("NIFTY")
    c.is_model = False  # pretend it came from an exchange
    fit = calibrate_chain(c)
    assert fit.n_used >= MIN_STRIKES
    assert fit.forward > c.spot          # forward carries the rate, spot does not
    assert -1.0 < fit.rho < 1.0 and fit.nu > 0


# -- the endpoint -------------------------------------------------------------


def test_endpoint_refuses_a_modelled_chain(client):
    """Offline the chain is synthetic, so calibration must refuse rather than
    hand back a surface fitted to the generator's own parameters."""
    r = client.get("/api/viz/sabr/NIFTY")
    assert r.status_code == 422
    assert "modelled chain" in r.json()["detail"]


def test_endpoint_rejects_a_bad_expiry(client):
    r = client.get("/api/viz/sabr/NIFTY?expiry=next-thursday")
    assert r.status_code == 400
