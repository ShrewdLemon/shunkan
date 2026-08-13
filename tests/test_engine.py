import numpy as np
import pandas as pd
import pytest

from shunkan.backtest import (
    BacktestConfig,
    STRATEGIES,
    get_strategy,
    grid_search,
    run_backtest,
)

ZERO_COST = BacktestConfig(commission=0.0, slippage=0.0)


def _frame(closes):
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": np.full(len(closes), 1_000_000),
        },
        index=idx,
    )


def test_buy_hold_equity_matches_price_ratio():
    prices = _frame([100, 110, 99, 132, 120])
    signal = pd.Series(1.0, index=prices.index)
    result = run_backtest(prices, signal, ZERO_COST)
    assert result.final_equity == pytest.approx(
        ZERO_COST.initial_cash * 120 / 100, rel=1e-12
    )


def test_no_look_ahead_bias():
    # Big move happens at bar 2; a signal raised on bar 2 must NOT capture it.
    prices = _frame([100, 100, 200, 200, 200])
    signal = pd.Series([0, 0, 1, 0, 0], index=prices.index, dtype=float)
    result = run_backtest(prices, signal, ZERO_COST)
    # Position is held during bar 3 only; bar 3 return is 0 -> equity unchanged.
    assert result.final_equity == pytest.approx(ZERO_COST.initial_cash)


def test_short_profits_from_decline():
    prices = _frame([100, 100, 80, 80])
    signal = pd.Series([-1, -1, -1, -1], index=prices.index, dtype=float)
    result = run_backtest(prices, signal, ZERO_COST)
    # Short from bar1 close onward: gains 20% on the bar1->bar2 drop.
    assert result.final_equity == pytest.approx(ZERO_COST.initial_cash * 1.2)


def test_allow_short_false_clips_signal():
    prices = _frame([100, 100, 80, 80])
    signal = pd.Series([-1, -1, -1, -1], index=prices.index, dtype=float)
    cfg = BacktestConfig(commission=0.0, slippage=0.0, allow_short=False)
    result = run_backtest(prices, signal, cfg)
    assert result.final_equity == pytest.approx(cfg.initial_cash)


def test_costs_reduce_equity():
    prices = _frame([100, 110, 99, 132, 120])
    signal = pd.Series(1.0, index=prices.index)
    free = run_backtest(prices, signal, ZERO_COST)
    costly = run_backtest(
        prices, signal, BacktestConfig(commission=0.001, slippage=0.001)
    )
    assert costly.final_equity < free.final_equity


def test_trade_extraction_round_trip():
    prices = _frame([100, 100, 110, 120, 120, 120])
    signal = pd.Series([0, 1, 1, 0, 0, 0], index=prices.index, dtype=float)
    result = run_backtest(prices, signal, ZERO_COST)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.direction == 1
    # Entry at bar2 close (fill delay), exit at bar4 close.
    assert trade.entry_price == pytest.approx(110)
    assert trade.exit_price == pytest.approx(120)
    assert trade.return_pct == pytest.approx(120 / 110 - 1)


def test_all_strategies_produce_valid_signals(prices):
    for name, strat in STRATEGIES.items():
        sig = strat.signal(prices)
        assert not sig.isna().any(), f"{name} produced NaNs"
        assert set(np.sign(sig.unique())).issubset({-1.0, 0.0, 1.0}), name
        result = run_backtest(prices, sig, strategy_name=name)
        assert len(result.equity) == len(prices)
        assert result.final_equity > 0


def test_metrics_dictionary_complete(prices):
    strat = get_strategy("sma_cross")
    result = run_backtest(prices, strat.signal(prices))
    metrics = result.metrics()
    for key in ("sharpe", "max_drawdown", "cagr", "win_rate", "num_trades"):
        assert key in metrics


def test_grid_search_finds_best(prices):
    strat = get_strategy("sma_cross")
    opt = grid_search(
        prices, strat, metric="sharpe",
        param_grid={"fast": [5, 10, 20], "slow": [50, 100]},
    )
    assert opt.combos_tested == 6  # no degenerate combos here
    assert set(opt.best_params) == {"fast", "slow"}
    assert opt.table["sharpe"].iloc[0] == opt.table["sharpe"].max()


def test_grid_search_skips_degenerate_combos(prices):
    strat = get_strategy("sma_cross")
    opt = grid_search(
        prices, strat, param_grid={"fast": [50, 100], "slow": [50, 100]}
    )
    # Only fast=50/slow=100 is valid.
    assert opt.combos_tested == 1


def test_engine_speed_10y_daily_under_50ms(prices):
    # prices fixture is 2y; build 10y synthetic for the speed check
    from shunkan.data.provider import SyntheticProvider

    big = SyntheticProvider().history("SPEED", period="10y")
    strat = get_strategy("sma_cross")
    sig = strat.signal(big)
    result = run_backtest(big, sig)
    assert result.elapsed_ms < 50.0
