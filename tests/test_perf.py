"""Latency-budget tests — the 'fast like an HFT shop' contract.

Budgets are generous vs. typical timings (10-50x headroom) so CI noise
doesn't flake them, but they will catch any accidental O(n²) or per-row
Python loop sneaking into a hot path.
"""

import time

import numpy as np
import pytest

from shunkan.analytics.volume import analyze_volume
from shunkan.backtest import get_strategy, run_backtest
from shunkan.data.provider import SyntheticProvider
from shunkan.derivatives import analyze_chain, bs_greeks, implied_vol, synthetic_chain
from shunkan.intel.feeds import NewsItem
from shunkan.intel.impact import assess_impact
from shunkan.intel.sentiment import score_sentiment


def _timed(fn, *args, repeat=5, **kwargs):
    best = float("inf")
    out = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn(*args, **kwargs)
        best = min(best, time.perf_counter() - t0)
    return out, best * 1000.0  # ms


def test_full_chain_greeks_under_5ms():
    strikes = np.arange(20000.0, 26000.0, 50.0)  # 120 strikes
    _, ms = _timed(bs_greeks, 23200.0, strikes, 7 / 365, 0.14, True)
    assert ms < 5.0, f"greeks took {ms:.2f}ms"


def test_chain_iv_solve_under_25ms():
    chain = synthetic_chain("NIFTY", n_strikes=41)
    prices = np.concatenate([chain.call_ltp, chain.put_ltp])
    strikes = np.concatenate([chain.strikes, chain.strikes])
    is_call = np.concatenate([np.ones(41, bool), np.zeros(41, bool)])
    _, ms = _timed(implied_vol, prices, chain.spot, strikes, chain.t_years, is_call)
    assert ms < 25.0, f"IV solve took {ms:.2f}ms"


def test_chain_analytics_under_5ms():
    chain = synthetic_chain("NIFTY", n_strikes=41)
    chain.ensure_iv()
    _, ms = _timed(analyze_chain, chain)
    assert ms < 5.0, f"chain analytics took {ms:.2f}ms"


def test_backtest_10y_daily_under_25ms():
    big = SyntheticProvider().history("PERF", period="10y")
    sig = get_strategy("sma_cross").signal(big)
    _, ms = _timed(run_backtest, big, sig)
    assert ms < 25.0, f"backtest took {ms:.2f}ms"


def test_sentiment_under_100us_per_headline():
    text = "Nifty surges to record high as RBI cuts rates and banks rally sharply"
    n = 200
    t0 = time.perf_counter()
    for _ in range(n):
        score_sentiment(text)
    per_call_us = (time.perf_counter() - t0) / n * 1e6
    assert per_call_us < 100.0, f"sentiment took {per_call_us:.0f}µs/headline"


def test_impact_under_1ms_per_headline():
    item = NewsItem(title="RBI cuts repo rate in surprise move, banks surge",
                    link="", source="t", published=None)
    _, ms = _timed(assess_impact, item)
    assert ms < 1.0, f"impact call took {ms:.3f}ms"


def test_volume_analysis_under_25ms():
    hist = SyntheticProvider().history("PERF", period="2y")
    _, ms = _timed(analyze_volume, hist)
    assert ms < 25.0, f"volume analysis took {ms:.2f}ms"
