"""Monte Carlo bootstrap of backtest results.

A single backtest equity curve is one draw from a distribution. Block
bootstrap (resampling contiguous chunks of the strategy's daily returns,
preserving short-range autocorrelation) generates thousands of alternate
histories in one vectorized pass, giving honest confidence bands:

- P5/P50/P95 terminal equity and full equity envelopes
- probability of ending below break-even
- max-drawdown distribution (median and tail)

All numpy: 2,000 paths × 2,500 bars is a single (paths, bars) matrix.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class MonteCarloResult:
    n_paths: int
    n_bars: int
    block_size: int
    terminal_p5: float       # multiples of initial equity
    terminal_p50: float
    terminal_p95: float
    prob_loss: float         # P(terminal < 1.0)
    max_dd_median: float
    max_dd_p95: float        # 95th percentile worst drawdown (more negative)
    envelope_p5: np.ndarray = field(repr=False, default=None)
    envelope_p50: np.ndarray = field(repr=False, default=None)
    envelope_p95: np.ndarray = field(repr=False, default=None)
    elapsed_ms: float = 0.0

    def verdict(self) -> str:
        """What the path could have looked like. NOT whether the edge is real.

        This resamples the strategy's OWN realised returns, so whatever mean
        they had is baked into every path. A best-of-800 search over pure noise
        scored prob_loss 0.014 here and used to be told its "edge survives
        resampling", which was this method's wording and this method's fault.
        For the edge question see backtest.validate.
        """
        tail = f"path risk only — run backtest.validate for whether the edge is real"
        if self.prob_loss < 0.2 and self.max_dd_p95 > -0.35:
            return f"paths are mostly favourable, worst-case DD {self.max_dd_p95:.0%} — {tail}"
        if self.prob_loss < 0.45:
            return f"path outcome is close to a coin flip — {tail}"
        return f"most resampled paths lose money — {tail}"


def monte_carlo(
    returns: pd.Series,
    n_paths: int = 2000,
    block_size: int = 10,
    seed: int = 7,
) -> MonteCarloResult:
    """Block-bootstrap per-bar strategy returns into alternate equity paths."""
    import time

    t0 = time.perf_counter()
    r = returns.dropna().to_numpy(dtype=np.float64)
    n = len(r)
    if n < block_size * 4:
        raise ValueError(f"Need at least {block_size * 4} return bars, got {n}")

    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block_size))

    # (paths, blocks) random block starts -> expand to (paths, bars) indices.
    starts = rng.integers(0, n - block_size + 1, size=(n_paths, n_blocks))
    offsets = np.arange(block_size)
    idx = (starts[:, :, None] + offsets[None, None, :]).reshape(n_paths, -1)[:, :n]

    paths = r[idx]                                  # (paths, bars)
    equity = np.cumprod(1.0 + paths, axis=1)        # (paths, bars)

    terminal = equity[:, -1]
    peaks = np.maximum.accumulate(equity, axis=1)
    drawdowns = equity / peaks - 1.0
    max_dd = drawdowns.min(axis=1)                  # (paths,)

    env = np.percentile(equity, [5, 50, 95], axis=0)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    return MonteCarloResult(
        n_paths=n_paths,
        n_bars=n,
        block_size=block_size,
        terminal_p5=float(np.percentile(terminal, 5)),
        terminal_p50=float(np.percentile(terminal, 50)),
        terminal_p95=float(np.percentile(terminal, 95)),
        prob_loss=float((terminal < 1.0).mean()),
        max_dd_median=float(np.median(max_dd)),
        max_dd_p95=float(np.percentile(max_dd, 5)),  # 5th pct = worse tail
        envelope_p5=env[0],
        envelope_p50=env[1],
        envelope_p95=env[2],
        elapsed_ms=elapsed_ms,
    )
