"""VaR, efficient frontier, history archive, and bulk export."""

import numpy as np
import pandas as pd
import pytest

from shunkan.analytics.viz import efficient_frontier, var_analysis
from shunkan.store import HistoryArchive


def _closes(seed=11, n=300, cols=("A", "B", "C", "D")):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-01", periods=n)
    base = rng.normal(0.0004, 0.01, n)
    out = {}
    for i, c in enumerate(cols):
        r = base * (0.5 + 0.2 * i) + rng.normal(0, 0.008, n)
        out[c] = pd.Series(100 * np.cumprod(1 + r), index=idx)
    return out


# ---------------------------------------------------------------- VaR

@pytest.fixture(scope="module")
def var_result():
    return var_analysis(_closes())


def test_var_shapes(var_result):
    r = var_result
    n_h = len(r.horizons)
    assert r.var_curve.shape == (n_h,)
    assert r.surface.shape == (n_h, len(r.surface_bins))
    assert 0.999 <= r.surface.max() <= 1.0


def test_var_positive_and_growing_with_horizon(var_result):
    r = var_result
    assert (r.var_curve > 0).all()
    assert r.var_curve[-1] > r.var_curve[0]  # 34d risk > 1d risk


def test_es_at_least_var(var_result):
    assert (var_result.es_curve >= var_result.var_curve - 1e-12).all()


def test_var_deterministic():
    a, b = var_analysis(_closes()), var_analysis(_closes())
    np.testing.assert_array_equal(a.var_curve, b.var_curve)


def test_var_refuses_thin_history():
    thin = {k: v.iloc[:20] for k, v in _closes().items()}
    with pytest.raises(ValueError, match="overlapping daily returns"):
        var_analysis(thin)


# ---------------------------------------------------------------- frontier

@pytest.fixture(scope="module")
def frontier():
    return efficient_frontier(_closes())


def test_frontier_weights_sum_to_one(frontier):
    for p in (frontier.max_sharpe, frontier.min_vol):
        assert abs(sum(p["weights"].values()) - 1.0) < 1e-9
        assert all(w >= 0 for w in p["weights"].values())


def test_frontier_extremes_dominate_sample(frontier):
    pts = frontier.points  # (n, 3) vol, ret, sharpe
    assert frontier.max_sharpe["sharpe"] >= pts[:, 2].max() - 1e-9
    assert frontier.min_vol["vol"] <= pts[:, 0].min() + 1e-9


# ---------------------------------------------------------------- archive

def _ohlcv(n=30):
    idx = pd.bdate_range("2026-01-01", periods=n, tz="Asia/Kolkata")
    v = np.linspace(100, 110, n)
    return pd.DataFrame({"Open": v, "High": v + 1, "Low": v - 1,
                         "Close": v, "Volume": np.full(n, 1e6)}, index=idx)


def test_archive_upsert_merge_and_stats(tmp_path):
    arc = HistoryArchive(root=tmp_path)
    assert arc.upsert("NIFTY", _ohlcv(30), "yahoo/nse") == 30
    # overlapping re-sync must not duplicate rows
    assert arc.upsert("NIFTY", _ohlcv(30), "yahoo/nse") == 30
    df = arc.read("NIFTY")
    assert len(df) == 30 and "source" in df.columns
    s = arc.stats()
    assert s["symbols"]["NIFTY"]["rows"] == 30
    assert s["symbols"]["NIFTY"]["source"] == "yahoo/nse"


def test_archive_refuses_synthetic(tmp_path):
    arc = HistoryArchive(root=tmp_path)
    with pytest.raises(ValueError, match="synthetic"):
        arc.upsert("NIFTY", _ohlcv(5), "synthetic-demo")


# ---------------------------------------------------------------- export API

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from shunkan.server.api import create_app

    return TestClient(create_app())


def test_export_csv_long_format_with_source(client):
    r = client.get("/api/export/history?symbols=NIFTY,RELIANCE&period=6mo&fmt=csv")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    head, first = r.text.splitlines()[0], r.text.splitlines()[1]
    assert head.startswith("symbol,timestamp,")
    assert head.endswith(",source")
    # offline test mode must label itself, never masquerade as market data
    assert first.endswith("synthetic-demo")


def test_export_parquet_roundtrip(client, tmp_path):
    r = client.get("/api/export/history?symbols=NIFTY&period=6mo&fmt=parquet")
    assert r.status_code == 200
    p = tmp_path / "x.parquet"
    p.write_bytes(r.content)
    df = pd.read_parquet(p)
    assert {"symbol", "timestamp", "close", "source"} <= set(df.columns)
    assert (df["symbol"] == "NIFTY").all()


def test_export_rejects_bad_format(client):
    assert client.get("/api/export/history?symbols=NIFTY&fmt=xml").status_code == 400
