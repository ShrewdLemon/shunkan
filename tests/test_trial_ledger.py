import itertools

import pytest
from fastapi.testclient import TestClient

from shunkan.backtest import get_strategy
from shunkan.backtest.trials import TrialLedger
from shunkan.server import create_app


def test_distinct_configs_count_once(tmp_path):
    led = TrialLedger(tmp_path / "trials.json")
    assert led.record("nifty", {"s": "a", "p": 1}, 0.5) == 1
    assert led.record("NIFTY", {"p": 1, "s": "a"}, 0.5) == 1
    assert led.record("NIFTY", {"s": "a", "p": 2}, 1.5) == 2
    assert led.count("RELIANCE") == 0
    assert TrialLedger(tmp_path / "trials.json").count("NIFTY") == 2


def test_trials_floor_the_sharpe_spread():
    led = TrialLedger()
    led.record("X", {"a": 1}, 0.50)
    led.record("X", {"a": 2}, 0.51)
    t = led.trials("X")
    assert t.n_trials == 2 and t.sharpe_std == 1.0
    assert "2 configurations run on X" in t.source


def test_empty_symbol_is_one_trial():
    t = TrialLedger().trials("X")
    assert t.n_trials == 1 and t.sharpe_std is None


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_validate_counts_every_run_on_the_symbol(client):
    base = {"symbol": "RELIANCE", "strategy": "sma_cross", "period": "5y"}
    a = client.post("/api/backtest", json={**base, "params": {"fast": 10, "slow": 50}}).json()
    assert a["trials"]["n"] == 1
    client.post("/api/backtest", json={**base, "params": {"fast": 10, "slow": 50}})
    v = client.post("/api/backtest", json={**base, "params": {"fast": 20, "slow": 100},
                                           "mode": "validate"}).json()
    assert v["trials"]["n"] == 2
    assert v["deflated_sharpe"]["expected_max_from_trials"] > 0
    assert "RELIANCE" in v["trials"]["source"]


def test_walkforward_counts_the_combos_it_ran(client):
    from shunkan.backtest.optimize import _degenerate

    strat = get_strategy("sma_cross")
    grid = strat.param_grid
    ran = [c for c in itertools.product(*grid.values())
           if not _degenerate({**strat.defaults, **dict(zip(grid, c))})]
    wf = client.post("/api/backtest", json={"symbol": "TCS", "strategy": "sma_cross",
                                            "period": "10y", "mode": "walkforward"}).json()
    assert wf["trials"]["n"] == len(ran) < len(list(itertools.product(*grid.values())))


def test_failed_walkforward_is_a_400_and_adds_nothing(client):
    r = client.post("/api/backtest", json={"symbol": "HDFCBANK", "strategy": "sma_cross",
                                           "period": "6mo", "mode": "walkforward"})
    assert r.status_code == 400
    ok = client.post("/api/backtest", json={"symbol": "HDFCBANK", "strategy": "sma_cross"})
    assert ok.json()["trials"]["n"] == 1


def test_int_and_float_params_are_one_configuration(client):
    base = {"symbol": "SBIN", "strategy": "bollinger_breakout"}
    client.post("/api/backtest", json=base)
    r = client.post("/api/backtest", json={**base, "params": {"num_std": 2}}).json()
    assert r["trials"]["n"] == 1


def test_swarm_evaluations_count(client):
    d = client.post("/api/swarm", json={"symbol": "ITC", "strategy": "sma_cross",
                                        "particles": 8, "iters": 5}).json()
    best = d["best_params"]
    v = client.post("/api/backtest", json={"symbol": "ITC", "strategy": "sma_cross",
                                           "params": best, "mode": "validate"}).json()
    # n_evals also counts degenerate points (fast >= slow) that were never backtested
    assert 0.9 * d["n_evals"] <= v["trials"]["n"] <= d["n_evals"]


def test_builder_and_script_runs_count(client):
    spec = {"direction": "long",
            "long_entry": [{"left": {"indicator": "RSI", "period": 14}, "op": "<", "value": 35}],
            "long_exit": [{"left": {"indicator": "RSI", "period": 14}, "op": ">", "value": 65}]}
    b = client.post("/api/backtest/build", json={"symbol": "INFY", "period": "5y",
                                                 "spec": spec}).json()
    assert b["trials"]["n"] == 1
    s = client.post("/api/script/run", json={
        "symbol": "INFY", "period": "5y",
        "code": "fast = ema(close, 10)\nslow = ema(close, 40)\nlong_when(cross_above(fast, slow))\n"
                "exit_when(cross_below(fast, slow))"}).json()
    assert s["ok"] and "backtest" in s, s
    assert s["trials"]["n"] == 2


def test_offline_results_say_synthetic(client):
    base = {"symbol": "NIFTY", "strategy": "sma_cross", "period": "2y"}
    for mode in ("backtest", "validate", "montecarlo", "walkforward"):
        r = client.post("/api/backtest", json={**base, "mode": mode,
                                               "period": "10y" if mode == "walkforward" else "2y"}).json()
        assert r["synthetic"] is True, mode
        assert "Synthetic prices" in r["data_note"], mode
    ml = client.post("/api/ml/train", json={"symbol": "NIFTY", "period": "5y",
                                           "features": ["ret5", "rsi14"]})
    assert ml.status_code == 200, ml.text
    assert ml.json()["synthetic"] is True


def test_wrong_parameter_names_are_a_400_not_a_500(client):
    r = client.post("/api/backtest", json={"symbol": "NIFTY", "strategy": "bollinger_breakout",
                                           "params": {"fast": 20}})
    assert r.status_code == 400 and "fast" in r.json()["detail"]
    assert client.post("/api/backtest", json={"symbol": "NIFTY", "strategy": "sma_cross",
                                              "params": {"fast": 20}}).json()["trials"]["n"] == 1


def test_corrupt_ledger_files_do_not_break_the_app(tmp_path):
    p = tmp_path / "trials.json"
    p.write_bytes(b"\xff\xfe{}")
    assert TrialLedger(p).count("X") == 0
    p.write_text('{"NIFTY": {"a": "0.5", "b": true, "c": 1.5}, "BAD": [1]}')
    led = TrialLedger(p)
    assert led.count("NIFTY") == 3 and led.count("BAD") == 0
    led.record("NIFTY", {"new": 1}, 0.2)
    assert led.trials("NIFTY").n_trials == 4
