"""SABR: fit the smile the market is actually quoting.

Black-Scholes gives one vol per strike and no view on how they relate. That is
fine for pricing a single contract off its own quote and useless for anything
that needs the surface: interpolating a strike nobody trades, pricing a spread
whose legs sit on different parts of the smile, or saying whether today's skew
is rich against its own history.

SABR is the market standard for a single expiry because Hagan's approximation
is closed form, so a fit is milliseconds rather than a Monte Carlo. Four
parameters:

    alpha  overall vol level, roughly ATM vol
    beta   backbone, how vol moves when the forward moves. FIXED, not fitted.
    rho    spot/vol correlation, which tilts the smile. Equity indices sit
           strongly negative because a selloff raises vol.
    nu     vol of vol, which sets how much the smile curves.

beta is fixed rather than fitted because beta and rho are nearly
indistinguishable from a single smile: many (beta, rho) pairs fit the same
quotes about equally well, so fitting both finds noise and reports it as
structure. 0.5 is the usual choice for index options.

Everything here refuses rather than guesses. A fit needs enough real quotes to
be meaningful, and every result carries its own residuals so a bad fit is
visible instead of smooth.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# A smile fitted to fewer than this many strikes is describing noise. Three
# free parameters need meaningfully more than three points to mean anything.
MIN_STRIKES = 6

# Beyond this the quotes are wings nobody trades, where a one-tick price move
# swings implied vol by whole points and drags the whole fit with it.
MAX_LOG_MONEYNESS = 0.35


@dataclass
class SABRFit:
    """A calibrated smile, plus everything needed to judge it."""

    alpha: float
    beta: float
    rho: float
    nu: float
    forward: float
    t_years: float
    strikes: np.ndarray          # the strikes actually used
    market_iv: np.ndarray        # what the market quoted at those strikes
    model_iv: np.ndarray         # what the fit says at those strikes
    n_used: int
    n_available: int
    method: str = "Hagan 2002 lognormal, beta fixed"
    warnings: list[str] = field(default_factory=list)

    @property
    def residuals(self) -> np.ndarray:
        """Model minus market, in vol points. Signed on purpose: a smile that
        misses high on both wings is a different problem from one that tilts."""
        return (self.model_iv - self.market_iv) * 100.0

    @property
    def rmse_vol_points(self) -> float:
        return float(np.sqrt(np.mean(self.residuals ** 2)))

    @property
    def max_error_vol_points(self) -> float:
        return float(np.max(np.abs(self.residuals)))

    @property
    def good(self) -> bool:
        """Whether this fit is worth quoting.

        A quarter of a vol point RMSE is roughly a tick on a liquid index
        option. Past about half a point the model and the market disagree
        about the shape of the smile, not about rounding.
        """
        return self.rmse_vol_points <= 0.5

    @property
    def quality(self) -> str:
        r = self.rmse_vol_points
        if r <= 0.25:
            return "tight"
        if r <= 0.5:
            return "usable"
        return "poor"

    def iv(self, strike) -> np.ndarray:
        """Model implied vol at any strike, including ones nobody quoted."""
        return sabr_iv(self.forward, strike, self.t_years,
                       self.alpha, self.beta, self.rho, self.nu)


def sabr_iv(forward, strike, t_years, alpha, beta, rho, nu):
    """Hagan's lognormal implied vol. Vectorised over strike.

    The z/x(z) factor is 1 at the money and removable there, but computing it
    directly divides 0 by 0. The series expansion below is used near the money
    instead of special-casing exact equality, because a strike a rupee away
    from the forward is numerically just as bad as one sitting on it.
    """
    forward = float(forward)
    k = np.asarray(strike, dtype=np.float64)
    t = max(float(t_years), 1e-9)
    alpha = max(float(alpha), 1e-9)
    nu = max(float(nu), 1e-9)
    rho = float(np.clip(rho, -0.999, 0.999))

    log_fk = np.log(forward / k)
    fk_beta = (forward * k) ** ((1.0 - beta) / 2.0)

    # Denominator series in log-moneyness.
    denom = fk_beta * (1.0
                       + ((1.0 - beta) ** 2 / 24.0) * log_fk ** 2
                       + ((1.0 - beta) ** 4 / 1920.0) * log_fk ** 4)

    z = (nu / alpha) * fk_beta * log_fk
    # x(z) -> z as z -> 0, so z/x(z) -> 1. Expand rather than divide.
    sqrt_term = np.sqrt(1.0 - 2.0 * rho * z + z ** 2)
    x = np.log((sqrt_term + z - rho) / (1.0 - rho))
    small = np.abs(z) < 1e-7
    z_over_x = np.where(small, 1.0 + 0.5 * rho * z, z / np.where(small, 1.0, x))

    correction = 1.0 + t * (
        ((1.0 - beta) ** 2 / 24.0) * alpha ** 2 / (forward * k) ** (1.0 - beta)
        + 0.25 * rho * beta * nu * alpha / fk_beta
        + ((2.0 - 3.0 * rho ** 2) / 24.0) * nu ** 2
    )
    return (alpha / denom) * z_over_x * correction


def _pack(params):
    """Map unconstrained search space to valid SABR parameters.

    A plain least-squares walk will happily propose rho = 3 or a negative
    alpha, which produce NaNs rather than a bad score, and NaNs stall the
    optimiser instead of steering it. Squashing keeps every proposal legal.
    """
    a_raw, r_raw, n_raw = params
    alpha = float(np.exp(a_raw))
    rho = float(np.tanh(r_raw))
    nu = float(np.exp(n_raw))
    return alpha, rho, nu


def _unpack(alpha, rho, nu):
    return np.array([np.log(max(alpha, 1e-8)),
                     np.arctanh(np.clip(rho, -0.99, 0.99)),
                     np.log(max(nu, 1e-8))], dtype=np.float64)


def calibrate_sabr(forward, strikes, market_iv, t_years, beta: float = 0.5,
                   weights=None, n_available: int | None = None) -> SABRFit:
    """Fit alpha, rho and nu to a quoted smile. beta is an input, not an output.

    `weights` lets a caller down-weight strikes it trusts less; the chain
    calibrator uses open interest, because a vol printed off a contract nobody
    holds is a number, not a quote.

    Raises ValueError when there is not enough real data to fit. That is the
    honest outcome and it is the caller's job to show it, not to smooth it.
    """
    k = np.asarray(strikes, dtype=np.float64)
    iv = np.asarray(market_iv, dtype=np.float64)
    if k.shape != iv.shape:
        raise ValueError("strikes and market_iv must be the same length")

    n_available = len(k) if n_available is None else n_available
    w = np.ones_like(k) if weights is None else np.asarray(weights, dtype=np.float64)

    usable = np.isfinite(iv) & (iv > 1e-4) & np.isfinite(k) & (k > 0)
    usable &= np.abs(np.log(forward / np.where(k > 0, k, 1.0))) <= MAX_LOG_MONEYNESS
    if usable.sum() < MIN_STRIKES:
        raise ValueError(
            f"only {int(usable.sum())} usable quotes within "
            f"{MAX_LOG_MONEYNESS:.0%} log-moneyness, need {MIN_STRIKES} "
            "— the smile is too thin to fit"
        )
    k, iv, w = k[usable], iv[usable], w[usable]
    w = w / w.sum() if w.sum() > 0 else np.ones_like(w) / len(w)

    # Seed from the market rather than a constant: ATM vol is a good alpha,
    # index smiles are downward sloping so rho starts negative, and nu starts
    # where a typical index smile lives.
    atm_i = int(np.argmin(np.abs(k - forward)))
    seed = _unpack(alpha=float(iv[atm_i]) * forward ** (1.0 - beta),
                   rho=-0.3, nu=0.6)

    def loss(params):
        alpha, rho, nu = _pack(params)
        model = sabr_iv(forward, k, t_years, alpha, beta, rho, nu)
        if not np.all(np.isfinite(model)):
            return 1e9
        return float(np.sum(w * (model - iv) ** 2))

    best = _nelder_mead(loss, seed)
    alpha, rho, nu = _pack(best)
    model = sabr_iv(forward, k, t_years, alpha, beta, rho, nu)

    warnings: list[str] = []
    if abs(rho) > 0.95:
        warnings.append("rho pinned near its bound; the smile may be steeper "
                        "than SABR can represent at this beta")
    if nu > 3.0:
        warnings.append("very high vol-of-vol; check for a stale or crossed quote")
    if usable.sum() < n_available * 0.5:
        warnings.append(f"fitted on {int(usable.sum())} of {n_available} strikes; "
                        "the rest were illiquid, unquoted or deep wing")

    return SABRFit(alpha=alpha, beta=beta, rho=rho, nu=nu, forward=forward,
                   t_years=float(t_years), strikes=k, market_iv=iv,
                   model_iv=model, n_used=int(usable.sum()),
                   n_available=int(n_available), warnings=warnings)


def _nelder_mead(fn, x0, iters: int = 400, tol: float = 1e-12):
    """Small simplex search, so the package needs no scipy.

    Three parameters on a smooth surface with a market-informed seed: this
    converges in milliseconds and keeps the dependency list honest.
    """
    n = len(x0)
    sim = np.vstack([x0] + [x0 + 0.5 * np.eye(n)[i] for i in range(n)])
    val = np.array([fn(p) for p in sim])
    for _ in range(iters):
        order = np.argsort(val)
        sim, val = sim[order], val[order]
        if abs(val[-1] - val[0]) < tol:
            break
        centroid = sim[:-1].mean(axis=0)
        refl = centroid + (centroid - sim[-1])
        f_refl = fn(refl)
        if f_refl < val[0]:
            exp = centroid + 2.0 * (centroid - sim[-1])
            sim[-1], val[-1] = (exp, fn(exp)) if fn(exp) < f_refl else (refl, f_refl)
        elif f_refl < val[-2]:
            sim[-1], val[-1] = refl, f_refl
        else:
            con = centroid + 0.5 * (sim[-1] - centroid)
            f_con = fn(con)
            if f_con < val[-1]:
                sim[-1], val[-1] = con, f_con
            else:
                sim[1:] = sim[0] + 0.5 * (sim[1:] - sim[0])
                val[1:] = [fn(p) for p in sim[1:]]
    return sim[int(np.argmin(val))]


def calibrate_chain(chain, beta: float = 0.5, rate: float = 0.065) -> SABRFit:
    """Fit the smile a live OptionChain is quoting.

    Two decisions do most of the work here.

    Which quote to use per strike. Calls and puts on the same strike should
    imply the same vol and do not, because the far-OTM side is a few ticks
    wide and the ITM side barely trades. Taking the OTM wing on each side
    picks the contract the market is actually making a price in.

    Weighting by open interest. A vol solved off a contract nobody holds is a
    number, not a quote, and an unweighted fit lets a stale deep wing print
    drag the whole surface. Weighting by OI lets the strikes with real
    positioning set the shape.

    Refuses on a modelled chain. Fitting a model to synthetic quotes recovers
    the generator's own parameters and reports them as market structure, which
    is the exact failure this codebase exists to avoid.
    """
    if getattr(chain, "is_model", False):
        raise ValueError(
            "refusing to calibrate a modelled chain — the fit would recover "
            "the generator's parameters and render them as market structure"
        )

    chain.ensure_iv(rate)
    strikes = np.asarray(chain.strikes, dtype=np.float64)
    # Forward, not spot: the smile is quoted against what the underlying is
    # worth at expiry, and on a 7% rate a weekly is already a few points away.
    forward = float(chain.spot) * float(np.exp(rate * max(chain.t_years, 0.0)))

    otm_is_call = strikes >= forward
    iv = np.where(otm_is_call, chain.call_iv, chain.put_iv)
    oi = np.where(otm_is_call, chain.call_oi, chain.put_oi)

    return calibrate_sabr(forward=forward, strikes=strikes, market_iv=iv,
                          t_years=float(chain.t_years), beta=beta,
                          weights=np.maximum(oi, 0.0),
                          n_available=len(strikes))
