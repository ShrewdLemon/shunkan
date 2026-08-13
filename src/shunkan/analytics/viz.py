"""3D visualization grids — IV surface, greeks surfaces, Monte Carlo price fans.

Everything here produces plain numeric grids for the web terminal's WebGL
views. The rules of the house apply: vectorized numpy end to end, and every
grid states exactly which part is market data and which part is model
extension — a surface that can't explain itself doesn't ship.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from shunkan.derivatives.chain import OptionChain
from shunkan.derivatives.greeks import bs_greeks


# ---------------------------------------------------------------------------
# IV surface
# ---------------------------------------------------------------------------

@dataclass
class IVSurface:
    symbol: str
    spot: float
    strikes: np.ndarray          # (n_k,)
    days: np.ndarray             # (n_t,) calendar days to expiry
    iv: np.ndarray               # (n_t, n_k) implied vol in decimal
    atm_iv: float
    chain_days: float            # the one expiry that is actual market data
    source: str
    market_row: int = 0          # index into days that equals chain_days
    elapsed_ms: float = 0.0


def iv_surface(chain: OptionChain, max_days: int = 95, n_expiries: int = 14) -> IVSurface:
    """Build a strike x maturity IV surface anchored on a real chain smile.

    The smile at the chain's own expiry is market data (IVs solved from
    traded premiums). Other maturities are a model extension: the smile's
    deviation from ATM is damped by sqrt(T_chain / T) — the standard
    sticky-moneyness flattening — so the surface is honest about being
    one real slice plus a documented extrapolation.
    """
    t0 = time.perf_counter()
    chain.ensure_iv()

    smile = np.nanmean(np.vstack([chain.call_iv, chain.put_iv]), axis=0)
    valid = ~np.isnan(smile)
    strikes = chain.strikes[valid]
    smile = smile[valid]
    if len(strikes) < 5:
        raise ValueError(f"Too few strikes with solvable IV ({len(strikes)})")

    atm_iv = float(smile[np.argmin(np.abs(strikes - chain.spot))])
    chain_days = max(chain.t_years * 365.0, 1.0)

    days = np.unique(np.round(np.geomspace(
        max(chain_days * 0.35, 2.0), max_days, n_expiries)))
    # make sure the market expiry itself is one of the rows
    days = np.unique(np.append(days, np.round(chain_days)))

    damp = np.sqrt(chain_days / days)                       # (n_t,)
    iv = atm_iv + (smile[None, :] - atm_iv) * damp[:, None]  # (n_t, n_k)
    iv = np.maximum(iv, 0.01)

    market_row = int(np.argmin(np.abs(days - chain_days)))
    return IVSurface(
        symbol=chain.symbol, spot=chain.spot, strikes=strikes, days=days,
        iv=iv, atm_iv=atm_iv, chain_days=float(chain_days),
        source=chain.source, market_row=market_row,
        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
    )


# ---------------------------------------------------------------------------
# Greeks surface
# ---------------------------------------------------------------------------

GREEK_NAMES = ("delta", "gamma", "theta", "vega", "rho")


@dataclass
class GreeksSurface:
    greek: str
    is_call: bool
    spot: float
    sigma: float
    strikes: np.ndarray          # (n_k,)
    days: np.ndarray             # (n_t,)
    values: np.ndarray           # (n_t, n_k)
    elapsed_ms: float = 0.0


def greeks_surface(
    spot: float, sigma: float, greek: str = "gamma", is_call: bool = True,
    r: float = 0.065, span: float = 0.16, n_strikes: int = 48,
    max_days: int = 90, n_days: int = 44,
) -> GreeksSurface:
    """Black-Scholes greek over a strike x time grid at the live spot/IV.

    One broadcast bs_greeks call over the full meshgrid — no loops.
    """
    if greek not in GREEK_NAMES:
        raise ValueError(f"Unknown greek '{greek}'. Available: {', '.join(GREEK_NAMES)}")
    t0 = time.perf_counter()

    strikes = np.linspace(spot * (1 - span), spot * (1 + span), n_strikes)
    days = np.linspace(1.0, max_days, n_days)
    K, T = np.meshgrid(strikes, days / 365.0)               # (n_t, n_k) each

    values = bs_greeks(spot, K, T, sigma, is_call, r)[greek]
    return GreeksSurface(
        greek=greek, is_call=is_call, spot=spot, sigma=sigma,
        strikes=strikes, days=days, values=values,
        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
    )


def _aligned_returns(closes: dict[str, pd.Series]) -> pd.DataFrame:
    """Daily returns on a shared calendar-date index.

    Sources disagree on close timestamps (IST vs US close, tz-aware vs
    naive); a union of mismatched timestamps silently produces zero overlap.
    """
    normed = {}
    for sym, s in closes.items():
        idx = pd.to_datetime(s.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        ns = pd.Series(s.to_numpy(dtype=float), index=idx.normalize())
        normed[sym] = ns[~ns.index.duplicated(keep="last")]
    frame = pd.DataFrame(normed).sort_index()
    return frame.pct_change().dropna(how="all")


# ---------------------------------------------------------------------------
# Correlation matrix
# ---------------------------------------------------------------------------

@dataclass
class CorrelationResult:
    symbols: list[str]
    matrix: np.ndarray            # (n, n) Pearson corr of daily returns
    n_obs: int                    # overlapping return observations used
    avg_corr: float               # mean of off-diagonal cells (signed)
    top_pairs: list[tuple[str, str, float]] = field(default_factory=list)
    hedge_pairs: list[tuple[str, str, float]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0


def correlation_matrix(
    closes: dict[str, pd.Series], min_obs: int = 30, top_n: int = 5,
) -> CorrelationResult:
    """Pearson correlation of daily returns over the common date range.

    Symbols with fewer than min_obs overlapping returns are dropped and
    reported — a correlation from ten points is noise wearing a number.
    """
    t0 = time.perf_counter()
    returns = _aligned_returns(closes)
    counts = returns.notna().sum()
    dropped = [s for s in returns.columns if counts[s] < min_obs]
    returns = returns.drop(columns=dropped).dropna()
    symbols = list(returns.columns)
    if len(symbols) < 2:
        raise ValueError(
            f"Need >= 2 symbols with {min_obs}+ overlapping daily returns "
            f"(dropped: {', '.join(dropped) or 'none'})"
        )

    m = returns.corr().to_numpy()
    iu = np.triu_indices(len(symbols), k=1)
    off = m[iu]
    order = np.argsort(off)
    pair = lambda k: (symbols[iu[0][k]], symbols[iu[1][k]], float(off[k]))
    return CorrelationResult(
        symbols=symbols, matrix=m, n_obs=len(returns),
        avg_corr=float(off.mean()),
        top_pairs=[pair(k) for k in order[::-1][:top_n]],
        hedge_pairs=[pair(k) for k in order[:top_n]],
        dropped=dropped,
        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
    )


# ---------------------------------------------------------------------------
# Monte Carlo price fan
# ---------------------------------------------------------------------------

@dataclass
class PriceFan:
    symbol: str
    spot: float
    horizon_days: int
    n_paths: int
    block_size: int
    days: np.ndarray             # (h,) 1..horizon
    display_paths: np.ndarray    # (n_display, h) price levels, percentile-stratified
    envelope: dict[str, np.ndarray] = field(repr=False, default=None)  # p5..p95
    terminal_bins: np.ndarray = field(repr=False, default=None)        # bin centers
    terminal_freq: np.ndarray = field(repr=False, default=None)        # normalized
    prob_up: float = 0.0
    hist_bars: int = 0
    elapsed_ms: float = 0.0


def price_fan(
    history: pd.DataFrame, spot: float, symbol: str = "?",
    horizon_days: int = 60, n_paths: int = 2000, n_display: int = 48,
    block_size: int = 5, n_bins: int = 31, seed: int = 7,
) -> PriceFan:
    """Block-bootstrap real daily returns into forward price paths.

    Same resampling philosophy as the backtest Monte Carlo: contiguous
    blocks of the instrument's own return history, so fat tails and
    short-range autocorrelation survive. No normality assumed anywhere.
    """
    t0 = time.perf_counter()
    cols = {c.lower(): c for c in history.columns}
    close = history[cols["close"]].astype(float).to_numpy()
    r = np.diff(close) / close[:-1]
    r = r[np.isfinite(r)]
    if len(r) < block_size * 8:
        raise ValueError(f"Need at least {block_size * 8} return bars, got {len(r)}")

    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(horizon_days / block_size))
    starts = rng.integers(0, len(r) - block_size + 1, size=(n_paths, n_blocks))
    idx = (starts[:, :, None] + np.arange(block_size)[None, None, :]
           ).reshape(n_paths, -1)[:, :horizon_days]
    paths = spot * np.cumprod(1.0 + r[idx], axis=1)          # (n_paths, h)

    terminal = paths[:, -1]
    order = np.argsort(terminal)
    # evenly spaced through the sorted terminals -> a representative fan
    pick = order[np.linspace(0, n_paths - 1, n_display).astype(int)]

    env = {f"p{p}": np.percentile(paths, p, axis=0) for p in (5, 25, 50, 75, 95)}
    freq, edges = np.histogram(terminal, bins=n_bins)
    return PriceFan(
        symbol=symbol, spot=spot, horizon_days=horizon_days, n_paths=n_paths,
        block_size=block_size, days=np.arange(1, horizon_days + 1),
        display_paths=paths[pick], envelope=env,
        terminal_bins=(edges[:-1] + edges[1:]) / 2.0,
        terminal_freq=freq / freq.max(),
        prob_up=float((terminal > spot).mean()), hist_bars=len(r),
        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
    )


# ---------------------------------------------------------------------------
# Value at Risk — joint bootstrap of an equal-weight basket
# ---------------------------------------------------------------------------

@dataclass
class VarResult:
    symbols: list[str]
    horizons: np.ndarray          # (n_h,) sessions ahead
    var_curve: np.ndarray         # (n_h,) VaR at alpha, positive = loss fraction
    es_curve: np.ndarray          # (n_h,) expected shortfall beyond VaR
    p95_curve: np.ndarray         # (n_h,) upside 95th percentile
    surface_bins: np.ndarray      # (n_b,) P&L bin centers (fraction)
    surface: np.ndarray           # (n_h, n_b) normalized density
    alpha: float
    n_obs: int
    n_paths: int
    block_size: int
    elapsed_ms: float = 0.0


def var_analysis(
    closes: dict[str, pd.Series], horizons=(1, 2, 3, 5, 8, 13, 21, 34),
    n_paths: int = 4000, block_size: int = 5, alpha: float = 0.05,
    n_bins: int = 41, seed: int = 7,
) -> VarResult:
    """Historical-bootstrap VaR of the equal-weight basket.

    The basket return series is formed first (row mean across symbols), so
    the cross-correlation between names is embedded in every resampled
    day — no independence assumption between symbols, no normality.
    """
    t0 = time.perf_counter()
    returns = _aligned_returns(closes).dropna()
    if len(returns) < block_size * 8:
        raise ValueError(f"Need {block_size * 8}+ overlapping daily returns, "
                         f"got {len(returns)}")
    basket = returns.mean(axis=1).to_numpy()

    H = int(max(horizons))
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(H / block_size))
    starts = rng.integers(0, len(basket) - block_size + 1, size=(n_paths, n_blocks))
    idx = (starts[:, :, None] + np.arange(block_size)[None, None, :]
           ).reshape(n_paths, -1)[:, :H]
    growth = np.cumprod(1.0 + basket[idx], axis=1)      # (n_paths, H)

    hz = np.asarray(horizons, dtype=int)
    pnl = growth[:, hz - 1] - 1.0                       # (n_paths, n_h)

    var_curve = -np.percentile(pnl, alpha * 100, axis=0)
    es_curve = np.array([
        -pnl[pnl[:, j] <= -var_curve[j], j].mean() if (pnl[:, j] <= -var_curve[j]).any()
        else var_curve[j]
        for j in range(len(hz))
    ])
    p95_curve = np.percentile(pnl, 95, axis=0)

    lo, hi = float(pnl.min()), float(pnl.max())
    edges = np.linspace(lo, hi, n_bins + 1)
    surface = np.vstack([np.histogram(pnl[:, j], bins=edges)[0] for j in range(len(hz))])
    surface = surface / surface.max()

    return VarResult(
        symbols=list(returns.columns), horizons=hz,
        var_curve=var_curve, es_curve=es_curve, p95_curve=p95_curve,
        surface_bins=(edges[:-1] + edges[1:]) / 2.0, surface=surface,
        alpha=alpha, n_obs=len(returns), n_paths=n_paths,
        block_size=block_size, elapsed_ms=(time.perf_counter() - t0) * 1000.0,
    )


# ---------------------------------------------------------------------------
# Efficient frontier — random portfolios over the aligned universe
# ---------------------------------------------------------------------------

@dataclass
class FrontierResult:
    symbols: list[str]
    points: np.ndarray            # (n_show, 3) vol, ret, sharpe (annualized)
    max_sharpe: dict = field(default_factory=dict)
    min_vol: dict = field(default_factory=dict)
    rf: float = 0.065
    n_portfolios: int = 0
    n_obs: int = 0
    elapsed_ms: float = 0.0


def efficient_frontier(
    closes: dict[str, pd.Series], n_portfolios: int = 4000, rf: float = 0.065,
    n_show: int = 1500, seed: int = 7,
) -> FrontierResult:
    """Long-only random portfolios (Dirichlet weights) in one matrix pass.

    Annualized mean/covariance come from the shared calendar-date returns;
    Sharpe uses the Indian risk-free ballpark unless overridden.
    """
    t0 = time.perf_counter()
    returns = _aligned_returns(closes).dropna()
    if len(returns) < 60:
        raise ValueError(f"Need 60+ overlapping daily returns, got {len(returns)}")
    mu = returns.mean().to_numpy() * 252.0
    cov = returns.cov().to_numpy() * 252.0

    rng = np.random.default_rng(seed)
    W = rng.dirichlet(np.ones(len(mu)), size=n_portfolios)   # (n_p, n_sym)
    rets = W @ mu
    vols = np.sqrt(np.einsum("ij,jk,ik->i", W, cov, W))
    sharpes = (rets - rf) / np.maximum(vols, 1e-9)

    def port(i: int) -> dict:
        return {
            "weights": {s: float(w) for s, w in zip(returns.columns, W[i])},
            "ret": float(rets[i]), "vol": float(vols[i]), "sharpe": float(sharpes[i]),
        }

    pick = rng.choice(n_portfolios, size=min(n_show, n_portfolios), replace=False)
    points = np.column_stack([vols[pick], rets[pick], sharpes[pick]])
    return FrontierResult(
        symbols=list(returns.columns), points=points,
        max_sharpe=port(int(np.argmax(sharpes))), min_vol=port(int(np.argmin(vols))),
        rf=rf, n_portfolios=n_portfolios, n_obs=len(returns),
        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
    )
