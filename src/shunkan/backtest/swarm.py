"""Particle-swarm optimization of strategy parameters.

Swarm intelligence applied where it earns its keep: a PSO flock explores a
2-D parameter space where every fitness evaluation is a *real* vectorized
backtest (Sharpe after costs, next-bar fills — same engine, same honesty).
The full iteration history is recorded so the terminal can replay the swarm
converging over the fitness landscape in 3D.

Because parameters are integers (or coarse floats), evaluations are memoized:
a 24-particle x 30-iteration run typically needs only a few hundred unique
backtests, and the same cache fills the landscape grid for the surface plot.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from shunkan.analytics import stats
from shunkan.backtest.engine import BacktestConfig, run_backtest
from shunkan.backtest.strategies import Strategy

# Finite penalty for nonsensical combos (fast >= slow, oversold >= overbought):
# steers particles away without breaking velocity dynamics.
PENALTY = -5.0


@dataclass
class SwarmIteration:
    positions: np.ndarray        # (n_particles, 2) evaluated (rounded) coords
    fitness: np.ndarray          # (n_particles,)
    gbest: np.ndarray            # (2,)
    gbest_fitness: float


@dataclass
class SwarmResult:
    symbol: str
    strategy: str
    param_names: tuple[str, str]
    bounds: tuple[tuple[float, float], tuple[float, float]]
    integer_params: tuple[bool, bool]
    iterations: list[SwarmIteration] = field(repr=False, default_factory=list)
    landscape_x: np.ndarray = field(repr=False, default=None)   # (nx,)
    landscape_y: np.ndarray = field(repr=False, default=None)   # (ny,)
    landscape_z: np.ndarray = field(repr=False, default=None)   # (ny, nx) sharpe
    best_params: dict[str, Any] = field(default_factory=dict)
    best_fitness: float = float("-inf")
    best_metrics: dict[str, float] = field(default_factory=dict)
    n_evals: int = 0             # unique backtests actually run
    elapsed_ms: float = 0.0

    def verdict(self) -> str:
        if not self.iterations:
            return "no iterations"
        first = self.iterations[0].gbest_fitness
        final_gbest = self.iterations[-1].gbest_fitness
        gain = self.best_fitness - first
        plateau = sum(
            1 for it in self.iterations
            if abs(it.gbest_fitness - final_gbest) < 1e-9
        )
        conv = f"converged for the last {plateau}/{len(self.iterations)} iterations"
        if self.best_fitness <= 0:
            return f"no profitable region found — best Sharpe {self.best_fitness:.2f}; {conv}"
        if gain < 0.05:
            return (f"flat landscape — swarm's first guess was already near-best "
                    f"(+{gain:.2f} Sharpe gained); {conv}")
        return (f"swarm improved Sharpe by +{gain:.2f} over its initial best; {conv}")


def _pick_params(strategy: Strategy) -> tuple[str, str]:
    """First two numeric grid parameters define the search plane."""
    names = [k for k, v in strategy.param_grid.items()
             if v and all(isinstance(x, (int, float)) for x in v)]
    if len(names) < 2:
        raise ValueError(
            f"Strategy '{strategy.name}' needs >= 2 numeric parameters for a "
            f"swarm search plane, has {len(names)}"
        )
    return names[0], names[1]


def _bounds_from_grid(strategy: Strategy, name: str) -> tuple[float, float]:
    vals = strategy.param_grid[name]
    lo, hi = float(min(vals)), float(max(vals))
    pad = (hi - lo) * 0.15
    lo = max(lo - pad, 1.0 if isinstance(vals[0], int) else lo - pad)
    return lo, hi + pad


def _degenerate(params: dict[str, Any]) -> bool:
    if "fast" in params and "slow" in params and params["fast"] >= params["slow"]:
        return True
    if ("oversold" in params and "overbought" in params
            and params["oversold"] >= params["overbought"]):
        return True
    return False


def swarm_optimize(
    prices: pd.DataFrame,
    strategy: Strategy,
    symbol: str = "?",
    param_names: tuple[str, str] | None = None,
    n_particles: int = 24,
    n_iters: int = 30,
    landscape_res: int = 25,
    seed: int = 7,
    config: BacktestConfig | None = None,
) -> SwarmResult:
    """Run canonical PSO (inertia 0.72, cognitive/social 1.49) over 2 params."""
    t0 = time.perf_counter()
    cfg = config or BacktestConfig()
    px, py = param_names or _pick_params(strategy)
    bx, by = _bounds_from_grid(strategy, px), _bounds_from_grid(strategy, py)
    int_x = all(isinstance(v, int) for v in strategy.param_grid[px])
    int_y = all(isinstance(v, int) for v in strategy.param_grid[py])

    cache: dict[tuple, tuple[float, dict]] = {}

    def evaluate(x: float, y: float) -> tuple[tuple[float, float], float]:
        vx = int(round(x)) if int_x else round(float(x), 3)
        vy = int(round(y)) if int_y else round(float(y), 3)
        key = (vx, vy)
        if key not in cache:
            params = {**strategy.defaults, px: vx, py: vy}
            if _degenerate(params):
                cache[key] = (PENALTY, {})
            else:
                bt = run_backtest(prices, strategy.signal(prices, **params), cfg,
                                  symbol=symbol, strategy_name=strategy.name,
                                  params=params)
                sharpe = stats.sharpe(bt.returns)
                fit = PENALTY if not np.isfinite(sharpe) else float(sharpe)
                cache[key] = (fit, bt.metrics() if np.isfinite(sharpe) else {})
        return (float(vx), float(vy)), cache[key][0]

    rng = np.random.default_rng(seed)
    lo = np.array([bx[0], by[0]])
    hi = np.array([bx[1], by[1]])
    span = hi - lo

    pos = lo + rng.random((n_particles, 2)) * span
    vel = (rng.random((n_particles, 2)) - 0.5) * span * 0.2
    vmax = span * 0.25

    pbest = pos.copy()
    pbest_fit = np.full(n_particles, -np.inf)
    gbest = pos[0].copy()
    gbest_fit = -np.inf

    W, C1, C2 = 0.72, 1.49, 1.49
    iterations: list[SwarmIteration] = []

    for _ in range(n_iters):
        evald = np.empty_like(pos)
        fit = np.empty(n_particles)
        for i in range(n_particles):
            evald[i], fit[i] = evaluate(pos[i, 0], pos[i, 1])

        improved = fit > pbest_fit
        pbest[improved] = pos[improved]
        pbest_fit[improved] = fit[improved]
        best_i = int(np.argmax(pbest_fit))
        if pbest_fit[best_i] > gbest_fit:
            gbest_fit = float(pbest_fit[best_i])
            gbest = pbest[best_i].copy()

        iterations.append(SwarmIteration(
            positions=evald.copy(), fitness=fit.copy(),
            gbest=gbest.copy(), gbest_fitness=gbest_fit,
        ))

        r1, r2 = rng.random((2, n_particles, 2))
        vel = W * vel + C1 * r1 * (pbest - pos) + C2 * r2 * (gbest[None, :] - pos)
        vel = np.clip(vel, -vmax, vmax)
        pos = pos + vel
        # reflect at bounds so particles don't pile up on the walls
        over_lo, over_hi = pos < lo, pos > hi
        pos = np.where(over_lo, 2 * lo - pos, pos)
        pos = np.where(over_hi, 2 * hi - pos, pos)
        vel = np.where(over_lo | over_hi, -vel, vel)
        pos = np.clip(pos, lo, hi)

    # fitness landscape for the 3D surface — same cache, real backtests
    gx = np.linspace(bx[0], bx[1], landscape_res)
    gy = np.linspace(by[0], by[1], landscape_res)
    gz = np.empty((landscape_res, landscape_res))
    for j, yv in enumerate(gy):
        for i, xv in enumerate(gx):
            _, gz[j, i] = evaluate(xv, yv)

    # Best of EVERYTHING evaluated — swarm and landscape share the cache, and
    # every entry is a real backtest, so the landscape may legitimately beat
    # the swarm's own gbest on coarse grids. Report the true winner.
    best_key = max(cache, key=lambda k: cache[k][0])
    best_fit, best_metrics = cache[best_key]

    return SwarmResult(
        symbol=symbol, strategy=strategy.name, param_names=(px, py),
        bounds=(bx, by), integer_params=(int_x, int_y),
        iterations=iterations,
        landscape_x=gx, landscape_y=gy, landscape_z=gz,
        best_params={px: best_key[0], py: best_key[1]},
        best_fitness=best_fit, best_metrics=best_metrics,
        n_evals=len(cache),
        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
    )
