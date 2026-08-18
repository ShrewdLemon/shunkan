"""Replay, journal, migration, VWAP: the gaps the dogfood session named."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from shunkan.analytics.daily import (
    intraday_migration,
    journal_path,
    participants_asof,
    positioning_from_snapshot,
    read_journal,
    vwap_today,
    write_journal,
)


def _snapshot_frame(ts="2026-08-17T10:10:07+00:00", spot=24288.0, with_iv=False):
    strikes = np.arange(24100.0, 24600.0, 100.0)
    n = len(strikes)
    return pd.DataFrame({
        "ts": [ts] * n, "expiry": ["2026-08-18"] * n, "spot": [spot] * n,
        "strike": strikes,
        "call_oi": [1000, 2000, 8000, 3000, 9000],    # resist at 24500
        "put_oi": [7000, 9000, 4000, 2000, 1000],     # support at 24200
        "call_iv": ([0.15] * n if with_iv else [np.nan] * n),
        "put_iv": ([0.16] * n if with_iv else [np.nan] * n),
        "call_ltp": [0.0] * n, "put_ltp": [0.0] * n,
        "call_volume": [0.0] * n, "put_volume": [0.0] * n,
    })


def test_positioning_from_snapshot_reconstructs_the_map():
    out = positioning_from_snapshot(_snapshot_frame())
    assert out["support"] == 24200.0 and out["resistance"] == 24500.0
    assert out["max_pain"] is not None
    assert out["is_model"] is False
    assert "chain store snapshot" in out["source"]
    # pre-IV-era snapshot: the vol field refuses with the reason, not a guess
    assert out["atm_iv_pct"] is None
    assert "predates IV storage" in out["vol_note"]


def test_positioning_reads_ivs_when_the_era_has_them():
    out = positioning_from_snapshot(_snapshot_frame(with_iv=True))
    assert out["atm_iv_pct"] == pytest.approx(15.5, abs=0.01)


def test_journal_writes_once_and_refuses_overwrite(tmp_path):
    d = date(2026, 8, 18)
    assert write_journal("NIFTY", d, {"chart": {"close": 1.0}}, root=tmp_path)
    assert not write_journal("NIFTY", d, {"chart": {"close": 999.0}}, root=tmp_path)
    back = read_journal("NIFTY", d, root=tmp_path)
    assert back["chart"]["close"] == 1.0          # the record, not the rewrite
    assert back["_journal"]["day"] == "2026-08-18"
    assert journal_path("NIFTY", d, root=tmp_path).exists()


def test_participants_asof_sees_only_that_world(tmp_path):
    from shunkan.data.participant import store_path

    rows = []
    for i, day in enumerate([date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 17)]):
        for who in ("FII", "Client"):
            rows.append({"date": pd.Timestamp(day), "client_type": who,
                         "idx_fut_net": 100 * (i + 1), "idx_opt_net": -50 * (i + 1)})
    pd.DataFrame(rows).to_parquet(store_path(tmp_path), index=False)
    out = participants_asof(date(2026, 8, 14), root=tmp_path)
    assert out["date"] == "2026-08-14" and out["prev_date"] == "2026-08-13"
    assert out["by_participant"]["FII"]["idx_fut_net"] == 200   # not the 17th's 300


def test_vwap_refuses_zero_volume_tape(tmp_path):
    from shunkan.store import BarBuilder, TickStore

    bb = BarBuilder()
    t0 = 1_700_000_000
    bb.on_tick("NIFTY", 100.0, 0.0, ts=t0)
    bb.on_tick("NIFTY", 101.0, 0.0, ts=t0 + 61)
    bb.on_tick("NIFTY", 102.0, 0.0, ts=t0 + 122)
    TickStore(root=tmp_path).write_bars(bb.drain())
    val, note = vwap_today("NIFTY", root=tmp_path)
    assert val is None and "zero volume" in note


def test_vwap_computes_on_a_real_tape(tmp_path):
    from shunkan.store import BarBuilder, TickStore

    bb = BarBuilder()
    t0 = 1_700_000_000
    # cum volume must advance WITHIN a bar for the bar to carry volume
    bb.on_tick("RELIANCE", 100.0, 1000.0, ts=t0)
    bb.on_tick("RELIANCE", 105.0, 2500.0, ts=t0 + 30)   # same minute
    bb.on_tick("RELIANCE", 110.0, 3000.0, ts=t0 + 61)   # closes bar1: vol 1500 @105
    bb.on_tick("RELIANCE", 120.0, 3600.0, ts=t0 + 90)   # same minute
    bb.on_tick("RELIANCE", 125.0, 4000.0, ts=t0 + 122)  # closes bar2: vol 600 @120
    TickStore(root=tmp_path).write_bars(bb.drain())
    val, note = vwap_today("RELIANCE", root=tmp_path)
    assert val == pytest.approx((105.0 * 1500 + 120.0 * 600) / 2100, rel=1e-9)
    assert "bars" in note


def test_intraday_migration_tracks_the_pain_path(tmp_path):
    from shunkan.store.store import ChainStore

    cs = ChainStore(root=tmp_path)
    # two captures, pain moving as put OI collapses at the top strike
    a = _snapshot_frame(ts="2026-08-18T06:00:00+00:00", spot=24200.0)
    b = _snapshot_frame(ts="2026-08-18T07:00:00+00:00", spot=24150.0)
    b["put_oi"] = [15000, 9000, 4000, 2000, 1000]      # 24100 wall grows
    path = cs._path("NIFTY")
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.concat([a, b]).to_parquet(path, index=False)
    out = intraday_migration("NIFTY", root=tmp_path)
    assert out["n_snapshots"] == 2
    assert len(out["path"]) == 2
    assert out["path"][0]["max_pain"] is not None
    puts = dict((k, v) for k, v in out["walls_built"]["puts"])
    assert puts.get(24100.0) == 8000.0                  # the build, measured
    assert "FIRST capture" in out["note"]


def test_replay_endpoint_contract(client):
    r = client.get("/api/analysis/daily/NIFTY?on=2999-01-01")
    assert r.status_code == 400
    assert "not happened" in r.json()["detail"]
    r = client.get("/api/analysis/daily/NIFTY?on=not-a-date")
    assert r.status_code == 400
    # a past date composes with named sections whatever the stores hold
    d = client.get("/api/analysis/daily/NIFTY?on=2026-08-14").json()
    assert d["served_from"] in ("reconstructed from archives",) or \
        d["served_from"].startswith("journal recorded")
    for k in ("chart", "vol", "positioning", "participants", "news"):
        assert k in d


def test_intraday_endpoint_names_an_empty_day(client):
    d = client.get("/api/analysis/intraday/NIFTY").json()
    assert ("path" in d) or ("error" in d and "capture" in d["error"])


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from shunkan.server import create_app

    with TestClient(create_app()) as c:
        yield c


def test_vwap_falls_back_to_the_front_future_and_says_so(tmp_path):
    from shunkan.store import BarBuilder, TickStore

    bb = BarBuilder()
    t0 = 1_700_000_000
    # the index prints no volume...
    bb.on_tick("NIFTY", 24200.0, 0.0, ts=t0)
    bb.on_tick("NIFTY", 24210.0, 0.0, ts=t0 + 61)
    bb.on_tick("NIFTY", 24205.0, 0.0, ts=t0 + 122)
    # ...but its front future does
    bb.on_tick("NIFTYFUT", 24250.0, 1000.0, ts=t0)
    bb.on_tick("NIFTYFUT", 24260.0, 5000.0, ts=t0 + 30)
    bb.on_tick("NIFTYFUT", 24255.0, 6000.0, ts=t0 + 61)
    bb.on_tick("NIFTYFUT", 24270.0, 7000.0, ts=t0 + 90)
    bb.on_tick("NIFTYFUT", 24280.0, 8000.0, ts=t0 + 122)
    TickStore(root=tmp_path).write_bars(bb.drain())
    val, note = vwap_today("NIFTY", root=tmp_path)
    assert val is not None
    assert "front future" in note and "NIFTYFUT" in note
