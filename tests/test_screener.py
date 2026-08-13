import pytest

from shunkan.screener import run_screen
from shunkan.screener.screener import compute_metrics, parse_rule


def test_parse_rule_basic():
    assert parse_rule("rsi<30") == ("rsi", "<", 30.0)
    assert parse_rule("ret_1mo>=0.05") == ("ret_1mo", ">=", 0.05)
    assert parse_rule("RSI > 70") == ("rsi", ">", 70.0)


def test_parse_rule_bare_boolean():
    metric, op, value = parse_rule("above_sma200")
    assert metric == "above_sma200" and op == ">" and value == 0.5


def test_parse_rule_rejects_unknown_metric():
    with pytest.raises(ValueError, match="Unknown metric"):
        parse_rule("pe_ratio<10")


def test_parse_rule_rejects_garbage():
    with pytest.raises(ValueError):
        parse_rule("rsi <")


def test_compute_metrics_keys(prices):
    metrics = compute_metrics(prices)
    for key in ("price", "rsi", "ret_1mo", "vol_ann", "above_sma50", "from_high"):
        assert key in metrics
    assert metrics["price"] > 0
    assert 0 <= metrics["rsi"] <= 100
    assert metrics["from_high"] <= 0  # can't be above the period high


def test_run_screen_no_rules_passes_everything(provider):
    universe = ["AAA", "BBB", "CCC"]
    result = run_screen(provider, universe, [])
    assert len(result.table) == 3
    assert not result.errors


def test_run_screen_impossible_rule_filters_all(provider):
    result = run_screen(provider, ["AAA", "BBB"], ["rsi>200"])
    assert len(result.table) == 0


def test_run_screen_and_logic(provider):
    loose = run_screen(provider, ["AAA", "BBB", "CCC", "DDD"], ["rsi>0"])
    tight = run_screen(provider, ["AAA", "BBB", "CCC", "DDD"], ["rsi>0", "rsi<1"])
    assert len(tight.table) <= len(loose.table)
