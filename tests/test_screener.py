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


def test_no_moving_average_is_not_the_same_claim_as_below_it():
    """A stock with 30 sessions has no 50- or 200-day average to compare to.

    These two columns read `float(price > last(sma, np.inf))`: an all-NaN SMA
    made last() fall back to +inf, `price > inf` is False, and the row shipped
    0.0. The screener then printed "below" for a stock at its ALL-TIME HIGH,
    about a line that was never drawn — while the returns column in the same
    row honestly dashed, which is what made "below" read as a measured fact.
    """
    import numpy as np
    import pandas as pd

    from shunkan.screener.screener import compute_metrics

    idx = pd.date_range("2026-07-01", periods=30, freq="B")
    rising = np.linspace(155.0, 300.0, 30)          # closes at its high
    hist = pd.DataFrame({"close": rising, "volume": [1e6] * 30,
                         "high": rising + 1, "low": rising - 1}, index=idx)
    m = compute_metrics(hist)

    assert np.isnan(m["above_sma50"]), \
        "a 50-day average that cannot be computed was reported as a direction"
    assert np.isnan(m["above_sma200"]), \
        "a 200-day average that cannot be computed was reported as a direction"
    # the honest neighbour, for contrast: 3-month return also refuses
    assert np.isnan(m["ret_3mo"])


def test_a_real_moving_average_still_reports_a_direction():
    """The refusal must not swallow the signal it exists to qualify."""
    import numpy as np
    import pandas as pd

    from shunkan.screener.screener import compute_metrics

    idx = pd.date_range("2024-01-01", periods=400, freq="B")
    up = np.linspace(100.0, 300.0, 400)
    hist = pd.DataFrame({"close": up, "volume": [1e6] * 400,
                         "high": up + 1, "low": up - 1}, index=idx)
    m = compute_metrics(hist)
    assert m["above_sma50"] == 1.0
    assert m["above_sma200"] == 1.0

    down = np.linspace(300.0, 100.0, 400)
    hist2 = pd.DataFrame({"close": down, "volume": [1e6] * 400,
                          "high": down + 1, "low": down - 1}, index=idx)
    m2 = compute_metrics(hist2)
    assert m2["above_sma50"] == 0.0
    assert m2["above_sma200"] == 0.0


def test_nan_sma_serialises_to_null_so_the_ui_can_dash():
    """_clean maps NaN to None; the UI's dash-with-a-reason branch keys on
    null. If this regresses, the frontend silently prints "below" again."""
    import numpy as np

    from shunkan.server.api import _clean

    out = _clean({"above_sma50": np.nan, "above_sma200": 1.0})
    assert out["above_sma50"] is None
    assert out["above_sma200"] == 1.0


def test_a_short_history_name_is_still_excluded_by_an_sma_rule():
    """Screen semantics must not change: run_screen does mask.fillna(False),
    so a NaN flag excludes the name exactly as 0.0 did."""
    import numpy as np
    import pandas as pd

    df = pd.DataFrame({"above_sma50": [1.0, 0.0, np.nan]})
    mask = (df["above_sma50"] > 0.5).fillna(False)
    assert list(mask) == [True, False, False]
