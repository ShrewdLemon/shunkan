"""Data store tests: bar building, chain snapshots, ΔOI, IV rank honesty."""

from datetime import date, timedelta

import time

import numpy as np
import pandas as pd
import pytest

from shunkan.derivatives.synthetic import synthetic_chain
from shunkan.store import (
    BarBuilder,
    ChainStore,
    TickStore,
    atm_iv_history,
    chain_delta_oi,
    iv_rank_local,
    store_stats,
    straddle_series,
)
from shunkan.store.store import MIN_IV_RANK_DAYS, _day_str


def _real_chain(symbol="NIFTY", oi_bump=0.0, spot=None):
    """A synthetic chain re-labeled as a real source so the store accepts it
    (tests need deterministic 'real' observations)."""
    c = synthetic_chain(symbol, spot=spot)
    c.source = "Zerodha Kite (real-time)"  # store only accepts real sources
    c.is_model = False  # ...and only chains that declare themselves observed
    if oi_bump:
        c.call_oi = c.call_oi + oi_bump
        c.put_oi = c.put_oi + 2 * oi_bump
    return c


# -- bars ---------------------------------------------------------------------


def test_bar_builder_aggregates_ohlcv():
    bb = BarBuilder()
    t0 = 1_700_000_040  # minute-aligned epoch (divisible by 60)
    bb.on_tick("NIFTY", 100.0, 1000, ts=t0)
    bb.on_tick("NIFTY", 103.0, 1500, ts=t0 + 10)
    bb.on_tick("NIFTY", 99.0, 2200, ts=t0 + 40)
    bb.on_tick("NIFTY", 101.0, 2400, ts=t0 + 59)
    # next minute rolls the bar
    bb.on_tick("NIFTY", 102.0, 3000, ts=t0 + 65)
    bars = bb.drain()
    assert len(bars) == 1
    b = bars[0]
    assert (b.open, b.high, b.low, b.close) == (100.0, 103.0, 99.0, 101.0)
    assert b.volume == pytest.approx(2400 - 1000)  # cumulative-volume delta


def test_bar_builder_multi_symbol_isolation():
    bb = BarBuilder()
    t0 = 1_700_000_000
    bb.on_tick("A", 10.0, 100, ts=t0)
    bb.on_tick("B", 20.0, 200, ts=t0)
    bb.on_tick("A", 11.0, 150, ts=t0 + 61)
    bb.on_tick("B", 21.0, 260, ts=t0 + 61)
    bars = {b.symbol: b for b in bb.drain()}
    assert bars["A"].close == 10.0 and bars["B"].close == 20.0


def test_tick_store_bar_roundtrip(tmp_path):
    bb = BarBuilder()
    t0 = 1_700_000_000
    bb.on_tick("TEST", 50.0, 100, ts=t0)
    bb.on_tick("TEST", 51.0, 300, ts=t0 + 61)
    ts = TickStore(root=tmp_path)
    written = ts.write_bars(bb.drain())
    assert written == 1
    df = ts.read_bars("TEST")
    assert df is not None and len(df) == 1
    assert float(df["close"].iloc[0]) == 50.0
    # idempotent merge on re-write of same minute
    bb2 = BarBuilder()
    bb2.on_tick("TEST", 60.0, 100, ts=t0)
    bb2.on_tick("TEST", 61.0, 300, ts=t0 + 61)
    ts.write_bars(bb2.drain())
    df2 = ts.read_bars("TEST")
    assert len(df2) == 1  # same minute deduped, latest kept


# -- chain snapshots ------------------------------------------------------------


def test_chain_store_rejects_synthetic(tmp_path):
    cs = ChainStore(root=tmp_path)
    fake = synthetic_chain("NIFTY")  # source says synthetic
    cs.snapshot(fake)
    assert cs.snapshots_today("NIFTY") is None  # refused — store holds real only


def test_chain_snapshot_and_straddle_series(tmp_path):
    cs = ChainStore(root=tmp_path)
    cs.snapshot(_real_chain())
    df = cs.snapshots_today("NIFTY")
    assert df is not None and df["ts"].nunique() == 1
    series = straddle_series("NIFTY", root=tmp_path)
    assert len(series) == 1
    assert series[0]["value"] > 0
    assert series[0]["strike"] in df["strike"].values


def test_chain_delta_oi_intraday_basis(tmp_path, monkeypatch):
    import shunkan.store.store as st

    cs = ChainStore(root=tmp_path)
    first = _real_chain()
    cs.snapshot(first)
    # Second snapshot with bumped OI, forced distinct timestamp.
    later = _real_chain(oi_bump=5000.0)
    monkeypatch.setattr(
        st, "_now_utc",
        lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc) + timedelta(minutes=10),
    )
    cs.snapshot(later)

    delta = chain_delta_oi(later, root=tmp_path)
    assert delta is not None
    assert "intraday" in delta["basis"]
    valid = ~np.isnan(delta["delta_call"])
    assert valid.any()
    assert np.allclose(delta["delta_call"][valid], 5000.0)
    assert np.allclose(delta["delta_put"][valid], 10000.0)


def test_chain_delta_oi_none_without_basis(tmp_path):
    chain = _real_chain()
    assert chain_delta_oi(chain, root=tmp_path) is None  # honest: no basis, no number


# -- IV rank honesty --------------------------------------------------------------


def _write_history_days(root, n_days, symbol="NIFTY"):
    """Fabricate n_days of past chain files directly (test scaffolding)."""
    cs = ChainStore(root=root)
    for i in range(n_days):
        d = date.today() - timedelta(days=n_days - i)
        c = _real_chain(spot=23000.0 + 50 * i)
        path = cs._path(symbol, d)
        n = len(c.strikes)
        df = pd.DataFrame({
            "ts": [f"{d.isoformat()}T10:00:00+00:00"] * n,
            "expiry": [str(c.expiry)] * n, "spot": [c.spot] * n,
            "strike": c.strikes,
            "call_ltp": c.call_ltp, "call_oi": c.call_oi,
            "call_iv": c.call_iv + i * 0.001,  # drifting IV history
            "call_volume": c.call_volume,
            "put_ltp": c.put_ltp, "put_oi": c.put_oi,
            "put_iv": c.put_iv + i * 0.001, "put_volume": c.put_volume,
            "source": [c.source] * n,
        })
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)


def test_iv_rank_refuses_insufficient_history(tmp_path):
    _write_history_days(tmp_path, 5)
    rank = iv_rank_local("NIFTY", 0.15, root=tmp_path)
    assert rank["available"] is False
    assert rank["days_captured"] == 5
    assert rank["days_required"] == MIN_IV_RANK_DAYS
    assert "rank" not in rank  # no fabricated number, ever


def test_atm_iv_intraday_one_point_per_snapshot(tmp_path):
    """Multiple captures in a day become a path; a NaN-spot capture is
    absent from it, never interpolated."""
    from shunkan.store.store import atm_iv_intraday

    cs = ChainStore(root=tmp_path)
    c = _real_chain()
    cs.snapshot(c)
    time.sleep(1.1)          # ts has second resolution; force a distinct one
    cs.snapshot(c)
    pts = atm_iv_intraday("NIFTY", root=tmp_path)
    assert len(pts) == 2
    assert all(p["iv"] > 0 for p in pts)
    assert pts[0]["ts"] < pts[1]["ts"]


def test_iv_history_skips_nan_spot_snapshot(tmp_path):
    """The 2026-08-10 live capture stored spot=NaN (spot unavailable, honestly
    recorded). The reader must SKIP that day, not raise all-NA idxmin and 500
    every endpoint that walks the history — which is what happened."""
    _write_history_days(tmp_path, 4)
    cs = ChainStore(root=tmp_path)
    d = date.today() - timedelta(days=2)
    path = cs._path("NIFTY", d)
    df = pd.read_parquet(path)
    df["spot"] = np.nan
    df.to_parquet(path, index=False)
    hist = atm_iv_history("NIFTY", root=tmp_path)
    assert len(hist) == 3          # the NaN-spot day is absent, not fatal


def test_iv_rank_with_sufficient_history(tmp_path):
    _write_history_days(tmp_path, MIN_IV_RANK_DAYS + 5)
    hist = atm_iv_history("NIFTY", root=tmp_path)
    assert len(hist) == MIN_IV_RANK_DAYS + 5
    top = iv_rank_local("NIFTY", float(hist.max()) + 0.01, root=tmp_path)
    assert top["available"] and top["rank"] == 1.0
    bottom = iv_rank_local("NIFTY", float(hist.min()) - 0.01, root=tmp_path)
    assert bottom["rank"] == 0.0
    assert top["days_captured"] == MIN_IV_RANK_DAYS + 5


# -- stats ------------------------------------------------------------------------


def test_store_stats(tmp_path):
    _write_history_days(tmp_path, 3)
    bb = BarBuilder()
    t0 = 1_700_000_000
    bb.on_tick("NIFTY", 100.0, 10, ts=t0)
    bb.on_tick("NIFTY", 101.0, 20, ts=t0 + 61)
    TickStore(root=tmp_path).write_bars(bb.drain())
    s = store_stats(root=tmp_path)
    assert s["chains"]["NIFTY"]["days"] == 3
    assert s["bars"]["NIFTY"]["days"] == 1
    assert s["size_bytes"] > 0


def test_store_refuses_model_chain_with_real_source(tmp_path):
    """The old gate string-matched `source`, so relabelling a modelled chain
    slipped it past. `is_model` is the contract now."""
    from shunkan.derivatives.synthetic import synthetic_chain
    from shunkan.store import ChainStore

    c = synthetic_chain("NIFTY")
    c.source = "Zerodha Kite (real-time)"  # a real-looking lie
    assert c.is_model is True

    ChainStore(root=tmp_path).snapshot(c)
    assert ChainStore(root=tmp_path).snapshots_today("NIFTY") is None


def test_write_day_snapshot_never_overwrites_an_observed_day(tmp_path):
    """A day we actually watched is worth more than a reconstruction of it,
    and silently replacing one with the other makes the basis label a lie."""
    import pandas as pd
    from datetime import date
    from shunkan.store import ChainStore

    cs = ChainStore(root=tmp_path)
    day = date(2026, 8, 10)
    observed = pd.DataFrame({"ts": ["obs"], "expiry": ["2026-08-18"], "spot": [1.0],
                             "strike": [24500.0], "call_ltp": [1.0], "call_oi": [111.0],
                             "call_iv": [0.1], "call_volume": [1.0], "put_ltp": [1.0],
                             "put_oi": [1.0], "put_iv": [0.1], "put_volume": [1.0],
                             "source": ["real"]})
    cs.write_day_snapshot("NIFTY", day, observed)
    reconstructed = observed.assign(call_oi=[999.0], source=["backfill"])
    cs.write_day_snapshot("NIFTY", day, reconstructed)

    back = cs.last_snapshot_of_day("NIFTY", day)
    assert back["call_oi"].iloc[0] == 111.0
    assert back["source"].iloc[0] == "real"


def test_snapshot_dedup_must_key_on_expiry_not_just_timestamp(tmp_path):
    """Two expiries captured in the same wall-clock second are two different
    observations. Keying dedup on ts alone silently drops the second, so the
    archive looks like it has term structure when it has one expiry repeated.
    """
    from datetime import date

    import numpy as np

    from shunkan.derivatives.synthetic import synthetic_chain
    from shunkan.store import ChainStore

    cs = ChainStore(root=tmp_path)
    near = synthetic_chain("NIFTY", expiry=date(2026, 8, 18))
    far = synthetic_chain("NIFTY", expiry=date(2026, 9, 29))
    for c in (near, far):
        c.is_model = False          # pretend both came from an exchange
        c.source = "Zerodha Kite (real-time)"

    cs.snapshot(near)
    cs.snapshot(far)
    got = cs.snapshots_today("NIFTY")
    assert got is not None
    assert set(got["expiry"]) == {"2026-08-18", "2026-09-29"}, (
        "second expiry was dropped: dedup is keying on ts alone")
