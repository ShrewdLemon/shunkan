"""Tests that can actually fail a strategy.

The existing Monte Carlo resamples the strategy's own realised returns. That
answers a real question, but not the one it was being read as. If a strategy
was selected because its returns had a positive mean, every resampled path
inherits that mean, so `prob_loss` comes out low by construction. A best-of-800
search over pure noise scored prob_loss 0.014 and was told its "edge survives
resampling". The bootstrap was never wrong; it was answering "how variable
could this path have been" and being read as "is this edge real".

Two tests here answer the second question, and they fail things.

PERMUTATION. Keep the market's own bar returns and shuffle WHEN the strategy
was in the market, in blocks so holding periods survive. If the timing carries
information, the true ordering should beat most random placements of the same
exposure profile. If it does not, the strategy was just harvesting drift or
market beta and the p-value says so.

DEFLATED SHARPE (Bailey & Lopez de Prado, 2014). The best of N trials has a
high Sharpe even when every trial is noise, because the maximum of N draws
grows with N. This corrects the observed Sharpe for how many things were tried,
plus the skew and fat tails of the return series. It is the direct answer to
"I searched 800 parameter sets and kept the winner".

Neither is a substitute for out-of-sample data. They are what you run before
you have any.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from shunkan.derivatives.greeks import norm_cdf

EULER_MASCHERONI = 0.5772156649015329


def sharpe(returns: np.ndarray, periods: int = 252) -> float:
    """Annualised Sharpe against a zero cash rate.

    Zero, not the Indian repo rate, on purpose: every comparison here is
    between this strategy and a permuted version of itself, so a constant
    offset cancels. Do not read this number as a standalone Sharpe.
    """
    r = np.asarray(returns, dtype=np.float64)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return 0.0
    sd = r.std(ddof=1)
    return 0.0 if sd <= 1e-12 else float(r.mean() / sd * math.sqrt(periods))


@dataclass
class PermutationResult:
    observed_sharpe: float
    null_sharpes: np.ndarray
    p_value: float           # P(a random placement does at least this well)
    n_permutations: int
    block_size: int

    @property
    def percentile(self) -> float:
        return float((self.null_sharpes < self.observed_sharpe).mean() * 100.0)

    @property
    def significant(self) -> bool:
        return self.p_value < 0.05

    def verdict(self) -> str:
        # Never says "edge". This test cannot see selection bias: a strategy
        # picked as best-of-N ON THIS SERIES beats random placement on it by
        # construction, and scores p=0.002 on pure noise. Pair with the
        # deflated Sharpe, which is the test that catches that.
        if self.p_value < 0.01:
            return (f"fits this series better than chance (p={self.p_value:.3f}) — "
                    "says nothing about selection; check the deflated Sharpe")
        if self.p_value < 0.05:
            return f"fits better than chance (p={self.p_value:.3f}), but not decisively"
        if self.p_value < 0.20:
            return (f"cannot distinguish this from luck (p={self.p_value:.3f}) — random "
                    "placements of the same exposure do about as well")
        return (f"no evidence of timing skill (p={self.p_value:.3f}) — this is drift or "
                "market exposure, not a signal")


def permutation_test(positions: pd.Series, bar_returns: pd.Series,
                     n_permutations: int = 1000, block_size: int = 20,
                     seed: int = 7, periods: int = 252) -> PermutationResult:
    """Shuffle the exposure, keep the market. Block-wise, so holding structure
    survives the shuffle.

    A plain element-wise shuffle would destroy the autocorrelation of the
    position series: a strategy that holds for twenty bars becomes twenty
    one-bar bets, which is a different strategy competing against the real one.
    The null has to be "same exposure profile, same holding periods, placed at
    random times".
    """
    pos = np.asarray(positions, dtype=np.float64)
    ret = np.asarray(bar_returns, dtype=np.float64)
    n = min(len(pos), len(ret))
    pos, ret = pos[:n], ret[:n]
    good = np.isfinite(pos) & np.isfinite(ret)
    pos, ret = pos[good], ret[good]
    if len(pos) < block_size * 4:
        raise ValueError(f"need at least {block_size * 4} bars, got {len(pos)}")

    observed = sharpe(pos * ret, periods)

    n_blocks = int(np.ceil(len(pos) / block_size))
    padded = np.zeros(n_blocks * block_size)
    padded[:len(pos)] = pos
    blocks = padded.reshape(n_blocks, block_size)

    rng = np.random.default_rng(seed)
    nulls = np.empty(n_permutations)
    for i in range(n_permutations):
        shuffled = blocks[rng.permutation(n_blocks)].ravel()[:len(pos)]
        nulls[i] = sharpe(shuffled * ret, periods)

    # +1 in both parts: the observed ordering is itself one of the possible
    # arrangements, so a p-value of exactly zero is not attainable and should
    # not be reportable.
    p = float((np.sum(nulls >= observed) + 1) / (n_permutations + 1))
    return PermutationResult(observed_sharpe=observed, null_sharpes=nulls,
                             p_value=p, n_permutations=n_permutations,
                             block_size=block_size)


@dataclass
class DeflatedSharpeResult:
    observed_sharpe: float
    expected_max_sharpe: float   # what the best of n_trials scores on pure noise
    deflated: float              # P(true Sharpe > 0) after correcting for the search
    n_trials: int
    n_obs: int
    skew: float
    kurtosis: float

    @property
    def survives(self) -> bool:
        return self.deflated > 0.95

    def verdict(self) -> str:
        if self.observed_sharpe <= self.expected_max_sharpe:
            return (f"Sharpe {self.observed_sharpe:.2f} is below the {self.expected_max_sharpe:.2f} "
                    f"a search over {self.n_trials} coin flips would produce — this is "
                    "the selection, not a strategy")
        if self.deflated > 0.95:
            return (f"survives deflation (DSR={self.deflated:.3f}) — the Sharpe is high "
                    f"even accounting for {self.n_trials} trials")
        if self.deflated > 0.80:
            return f"marginal after deflation (DSR={self.deflated:.3f}) — treat as unproven"
        return (f"does not survive deflation (DSR={self.deflated:.3f}) — the result is "
                f"consistent with picking the best of {self.n_trials} tries")


def deflated_sharpe(returns: pd.Series, n_trials: int,
                    trial_sharpe_std: float | None = None,
                    periods: int = 252) -> DeflatedSharpeResult:
    """Correct an observed Sharpe for the number of strategies that were tried.

    `n_trials` is the honest count of everything the search touched, not the
    number kept. An optimiser that evaluated 800 parameter sets has n_trials
    800 even if it reported one result, and understating it is the single
    easiest way to make this test say what you want.

    `trial_sharpe_std` is the spread of Sharpes across those trials. When the
    caller knows it (an optimiser does) it should pass it, because the
    expected maximum depends on it directly. The fallback assumes 1.0, which
    is deliberately conservative for daily strategies.
    """
    r = np.asarray(returns, dtype=np.float64)
    r = r[np.isfinite(r)]
    if len(r) < 20:
        raise ValueError(f"need at least 20 return observations, got {len(r)}")

    obs = sharpe(r, periods)
    n = len(r)
    sd = r.std(ddof=1)
    g3 = float(((r - r.mean()) ** 3).mean() / sd ** 3) if sd > 1e-12 else 0.0
    g4 = float(((r - r.mean()) ** 4).mean() / sd ** 4) if sd > 1e-12 else 3.0

    # Expected maximum Sharpe from n_trials draws of a zero-skill strategy.
    # The maximum of n normal draws grows like sqrt(2 ln n); this is the
    # sharper form from the paper.
    v = trial_sharpe_std if trial_sharpe_std and trial_sharpe_std > 0 else 1.0
    trials = max(int(n_trials), 1)
    if trials == 1:
        expected_max = 0.0
    else:
        z1 = _norm_ppf(1.0 - 1.0 / trials)
        z2 = _norm_ppf(1.0 - 1.0 / (trials * math.e))
        expected_max = v * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)

    # Per-observation Sharpe for the test statistic; the annualisation factor
    # would otherwise inflate the numerator without touching its standard error.
    sr_hat = obs / math.sqrt(periods)
    sr_0 = expected_max / math.sqrt(periods)
    denom = 1.0 - g3 * sr_hat + (g4 - 1.0) / 4.0 * sr_hat ** 2
    if denom <= 0:
        # Fat tails can push this negative, at which point the statistic is not
        # defined and reporting a number would be inventing one.
        deflated = float("nan")
    else:
        deflated = float(norm_cdf((sr_hat - sr_0) * math.sqrt(n - 1) / math.sqrt(denom)))

    return DeflatedSharpeResult(observed_sharpe=obs, expected_max_sharpe=expected_max,
                                deflated=deflated, n_trials=trials, n_obs=n,
                                skew=g3, kurtosis=g4)


def _norm_ppf(p: float) -> float:
    """Inverse normal CDF, Acklam's rational approximation.

    Kept local so the package keeps working without scipy, which it has avoided
    everywhere else.
    """
    p = min(max(p, 1e-12), 1.0 - 1e-12)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# ---------------------------------------------------------------------------
# Where the trial count comes from
# ---------------------------------------------------------------------------
#
# The deflated Sharpe is only as honest as n_trials, and a hand-supplied count
# is the easiest way to make it say what you want: pass 1 and a best-of-800
# winner sails through. Every search in this package already knows how many
# candidates it evaluated, so it reports that itself and callers stop guessing.


@dataclass
class SearchTrials:
    """What a parameter search actually did, in the terms deflation needs."""

    n_trials: int
    sharpe_std: float | None = None   # spread of Sharpe across the candidates
    source: str = ""                  # which search, for the provenance line

    @classmethod
    def single(cls) -> "SearchTrials":
        """One hypothesis, no search. Nothing to deflate."""
        return cls(n_trials=1, sharpe_std=None, source="single backtest")


def trials_of(search) -> SearchTrials:
    """Read the honest trial count off a search result.

    Accepts an OptimizationResult, a SwarmResult, a plain int, or None. The int
    path stays for callers that genuinely know better, but nothing inside this
    package uses it any more.
    """
    if search is None:
        return SearchTrials.single()
    if isinstance(search, int):
        return SearchTrials(n_trials=max(search, 1), source="caller-supplied")

    # Grid search: one row per combo, so the table carries both numbers.
    table = getattr(search, "table", None)
    combos = getattr(search, "combos_tested", None)
    if combos is not None:
        std = None
        if table is not None and "sharpe" in getattr(table, "columns", []):
            col = table["sharpe"].to_numpy(dtype=np.float64)
            col = col[np.isfinite(col)]
            if len(col) > 1:
                std = float(col.std(ddof=1))
        return SearchTrials(n_trials=max(int(combos), 1), sharpe_std=std,
                            source=f"grid search, {combos} combos")

    # Swarm: n_evals counts UNIQUE backtests. Memoised repeats are the same
    # parameter set, so they are not extra independent chances to get lucky
    # and must not inflate the count.
    evals = getattr(search, "n_evals", None)
    if evals is not None:
        std = None
        fits = [f for it in getattr(search, "iterations", [])
                for f in np.asarray(it.fitness, dtype=np.float64).ravel()]
        fits = np.array([f for f in fits if np.isfinite(f)])
        if len(fits) > 1:
            std = float(fits.std(ddof=1))
        return SearchTrials(n_trials=max(int(evals), 1), sharpe_std=std,
                            source=f"swarm, {evals} unique evaluations")

    raise TypeError(f"cannot read a trial count from {type(search).__name__}")


# ---------------------------------------------------------------------------
# The combined gate
# ---------------------------------------------------------------------------


@dataclass
class Validation:
    permutation: PermutationResult
    deflation: DeflatedSharpeResult
    trials: SearchTrials | None = None

    @property
    def passes(self) -> bool:
        """Both, or neither. Measured on synthetic cases:

            long-only drift        perm p=1.000  ->  fails, correctly
            unselected random      perm p=0.771  ->  fails, correctly
            genuine 60% hit rate   perm p=0.002  ->  passes, correctly
            best-of-800 on noise   perm p=0.002  ->  PASSES, wrongly
                                   DSR  0.000    ->  fails, correctly

        The permutation test cannot see selection bias and the deflated Sharpe
        cannot see whether the timing does anything. Requiring one of them is
        how a noise strategy gets a green light.
        """
        return self.permutation.significant and self.deflation.survives

    @property
    def basis(self) -> str:
        """Say what the deflation was measured against. A pass at one trial and
        a pass at eight hundred are different claims and must not read alike."""
        t = self.trials
        return f" [{t.source}]" if t and t.source else ""

    def verdict(self) -> str:
        if self.passes:
            return ("passes both: the timing fits better than chance AND the Sharpe "
                    "survives the number of trials. Still in-sample — this is what "
                    "you run before you have out-of-sample data, not instead of it."
                    + self.basis)
        fails = []
        if not self.permutation.significant:
            fails.append(f"timing is not distinguishable from random placement "
                         f"(p={self.permutation.p_value:.3f})")
        if not self.deflation.survives:
            fails.append(f"Sharpe does not survive {self.deflation.n_trials} trials "
                         f"(DSR={self.deflation.deflated:.3f})")
        return "rejected: " + "; and ".join(fails) + self.basis


def validate(result, search=None, bar_returns=None,
             n_permutations: int = 1000, periods: int = 252) -> Validation:
    """Run both tests on a BacktestResult.

    `search` is the OptimizationResult or SwarmResult that produced this
    strategy, and the trial count is read off it. Pass None only when the
    strategy really was a single hypothesis with no search behind it, because
    that is the assumption that makes deflation say yes.
    """
    t = trials_of(search)
    if bar_returns is None:
        # Back out the market's own bar returns from the strategy's, so the
        # permutation null is the real series and not a reconstruction.
        pos = result.positions.replace(0, np.nan)
        bar_returns = (result.returns / pos).fillna(0.0)
    return Validation(
        permutation=permutation_test(result.positions, bar_returns,
                                     n_permutations=n_permutations, periods=periods),
        deflation=deflated_sharpe(result.returns, n_trials=t.n_trials,
                                  trial_sharpe_std=t.sharpe_std, periods=periods),
        trials=t,
    )
