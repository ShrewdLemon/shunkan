"""Fund store: the scheme layer, and the join that makes it useful."""

import pandas as pd
import pytest

from shunkan.data.funds import _norm, funds_dir, schemes_holding, search_schemes
from shunkan.data.provider import DataError


def _seed(tmp_path):
    d = funds_dir(tmp_path)
    pd.DataFrame([
        {"isin": "INF1", "name": "Alpha Small Cap Fund", "amc": "Alpha",
         "category": "Small Cap", "aum": 5000.0, "benchmark": "NIFTY SMALLCAP"},
        {"isin": "INF2", "name": "Beta Nifty 50 ETF", "amc": "Beta",
         "category": "Index", "aum": 20000.0, "benchmark": "NIFTY 50 - TRI"},
    ]).to_parquet(d / "schemes.parquet", index=False)
    pd.DataFrame([
        {"isin": "INF1", "scheme_name": "Alpha Small Cap Fund", "holding": "Acme Ltd",
         "symbol": "ACME", "sector": "Industrials", "market_value_cr": 120.0,
         "weight_pct": 2.4, "change_1m_pct": 0.3, "as_of": "2026-08-20"},
        {"isin": "INF2", "scheme_name": "Beta Nifty 50 ETF", "holding": "Acme Ltd",
         "symbol": "ACME", "sector": "Industrials", "market_value_cr": 900.0,
         "weight_pct": 4.5, "change_1m_pct": -0.1, "as_of": "2026-08-20"},
        {"isin": "INF1", "scheme_name": "Alpha Small Cap Fund", "holding": "Treps",
         "symbol": None, "sector": None, "market_value_cr": 30.0,
         "weight_pct": 0.6, "change_1m_pct": None, "as_of": "2026-08-20"},
    ]).to_parquet(d / "holdings.parquet", index=False)


def test_holding_names_normalise_to_a_comparable_core():
    # vendor suffixes differ from exchange lists; the join depends on this
    assert _norm("Hitachi Energy India Ltd Ordinary Shares") == _norm("Hitachi Energy")
    assert _norm("Reliance Industries Limited") == _norm("Reliance Industries")


def test_schemes_holding_aggregates_and_dates_itself(tmp_path):
    _seed(tmp_path)
    d = schemes_holding("ACME", root=tmp_path)
    assert d["n_schemes"] == 2
    assert d["total_value_cr"] == 1020.0          # cash rows excluded by symbol
    assert d["as_of"] == "2026-08-20"
    assert d["schemes"][0]["value_cr"] == 900.0   # largest first
    assert {a["amc"] for a in d["by_amc"]} == {"Alpha", "Beta"}


def test_unresolved_cash_rows_never_become_a_stock(tmp_path):
    _seed(tmp_path)
    # "Treps" has no symbol; it must not answer as a holding of anything
    assert schemes_holding("TREPS", root=tmp_path)["n_schemes"] == 0


def test_search_ranks_by_size(tmp_path):
    _seed(tmp_path)
    rows = search_schemes("", root=tmp_path)
    assert rows[0]["aum_cr"] == 20000.0


def test_empty_store_says_so(tmp_path):
    with pytest.raises(DataError, match="import"):
        search_schemes("x", root=tmp_path / "nothing")
