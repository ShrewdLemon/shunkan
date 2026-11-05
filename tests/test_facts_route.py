"""The facts route, its refresh loop window, and the daily analysis's bhavcopy fallback."""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from shunkan.data import fo_bhav
from shunkan.markets import IST
from shunkan.server import api

NOV3 = date(2026, 11, 3)
SECTIONS = ("session", "price", "positioning", "flows", "expiry", "calendar")
BHAV_SRC = "NSE F&O bhavcopy 2026-11-03 (end of day)"
COLS = ["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "OpnPric",
        "HghPric", "LwPric", "ClsPric", "SttlmPric", "TtlTradgVol", "OpnIntrst",
        "ChngInOpnIntrst", "UndrlygPric", "NewBrdLotQty"]
ROWS = [("IDO", "2026-11-09", 23900, "CE", 180, 1_500_000), ("IDO", "2026-11-09", 23900, "PE", 45, 4_500_000),
        ("IDO", "2026-11-09", 24000, "CE", 110, 3_750_000), ("IDO", "2026-11-09", 24000, "PE", 75, 3_750_000),
        ("IDO", "2026-11-09", 24100, "CE", 60, 6_000_000), ("IDO", "2026-11-09", 24100, "PE", 125, 1_500_000),
        ("IDO", "2026-11-17", 24000, "CE", 150, 1_500_000), ("IDO", "2026-11-17", 24000, "PE", 120, 1_200_000),
        ("IDF", "2026-11-23", None, None, 24110, 12_000_000)]


def write_bhav(root, day=NOV3):
    lines = [",".join(COLS)]
    for tp, exp, k, right, px, oi in ROWS:
        lines.append(",".join([day.isoformat(), tp, "NIFTY", exp, "" if k is None else f"{k:.2f}",
                               right or "", *[str(px)] * 5, "10", str(oi), "0", "24025", "75"]))
    p = fo_bhav.raw_dir(root) / f"{day:%Y}" / f"BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
    p.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(p.name.removesuffix(".zip"), "\n".join(lines) + "\n")
    p.write_bytes(buf.getvalue())


def write_history(root, today_bar_captured=None):
    days = pd.bdate_range(end="2026-11-03", periods=300)
    closes = 22000 + 7 * np.arange(len(days)) + 60 * np.sin(np.arange(len(days)))
    df = pd.DataFrame({"date": days, "open": closes - 5, "high": closes + 30, "low": closes - 30,
                       "close": closes, "volume": 1000, "source": "yahoo",
                       "captured_at": [f"{d:%Y-%m-%d}T10:30:00+00:00" for d in days]})
    if today_bar_captured:
        df = pd.concat([df, pd.DataFrame([{
            "date": pd.Timestamp("2026-11-04"), "open": 24100.0, "high": 24200.0, "low": 24050.0,
            "close": 24150.0, "volume": 10, "source": "yahoo",
            "captured_at": today_bar_captured}])], ignore_index=True)
    p = root / "history" / "_NSEI.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)
    return float(closes[-1])


@pytest.fixture(scope="module")
def client():
    return TestClient(api.create_app())


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("shunkan.store.store.STORE_DIR", tmp_path)
    return tmp_path


def at(when):
    return lambda *a, **k: when


def chain(source, is_model):
    from shunkan.derivatives.synthetic import synthetic_chain

    c = synthetic_chain("NIFTY")
    c.source, c.is_model = source, is_model
    return c


def test_facts_route_serves_six_sections_offline(store, client):
    write_bhav(store)
    d = client.get("/api/facts/nifty").json()
    assert d["symbol"] == "NIFTY"
    for name in SECTIONS:
        sec = d[name]
        assert isinstance(sec, dict) and sec["source"], name
        assert "error" in sec or sec["as_of"], name
    assert d["session"]["as_of"] == "2026-11-03"
    assert d["positioning"]["source"] == BHAV_SRC and d["positioning"]["max_pain"] == 24000


def test_facts_route_on_an_empty_store_names_every_gap(store, client):
    d = client.get("/api/facts/NIFTY").json()
    for name in ("session", "price", "positioning", "flows", "expiry"):
        assert set(d[name]) == {"error", "source"}, name
    assert "calendar" in d


@pytest.mark.parametrize("make, reason", [
    (lambda: chain("synthetic (offline demo)", True), "live chain is modelled (synthetic (offline demo))"),
    (lambda: chain("NSE (live, ~1min delayed)", False), "live chain is from NSE (live, ~1min delayed), not Kite"),
])
def test_daily_positioning_uses_the_bhavcopy_when_the_chain_is_not_kite(store, client, monkeypatch,
                                                                       make, reason):
    write_bhav(store)
    monkeypatch.setattr("shunkan.data.chains.get_chain", lambda sym, *a: make())
    pos = client.get("/api/analysis/daily/NIFTY").json()["positioning"]
    assert pos["source"] == BHAV_SRC and pos["fallback_reason"] == reason
    assert pos["front_expiry"] == "2026-11-09" and pos["max_pain"] == 24000
    assert (pos["call_wall"], pos["put_wall"]) == (24100, 23900)


def unavailable(sym, *a):
    from shunkan.data.chains import ChainUnavailable

    raise ChainUnavailable(sym, ["Kite: no broker connected", "NSE: blocked"])


def test_daily_positioning_uses_the_bhavcopy_when_no_chain_resolves(store, client, monkeypatch):
    write_bhav(store)
    close = write_history(store)
    monkeypatch.setattr("shunkan.data.chains.get_chain", unavailable)
    monkeypatch.setattr("shunkan.markets.now_ist", at(datetime(2026, 11, 3, 20, 0, tzinfo=IST)))
    d = client.get("/api/analysis/daily/NIFTY").json()
    pos = d["positioning"]
    assert pos["source"] == BHAV_SRC
    assert pos["fallback_reason"] == "no live chain: Kite: no broker connected; NSE: blocked"
    assert d["as_of"] == "2026-11-03" and pos["spot"] == pytest.approx(close)
    assert pos["spot_source"] == "given"


def test_daily_positioning_keeps_the_error_without_a_bhavcopy(store, client, monkeypatch):
    monkeypatch.setattr("shunkan.data.chains.get_chain", unavailable)
    pos = client.get("/api/analysis/daily/NIFTY").json()["positioning"]
    assert "No live option chain" in pos["error"]
    assert pos["source_trail"] == ["Kite: no broker connected", "NSE: blocked"]
    assert pos["bhav_error"] == "no F&O bhavcopy on disk"


def test_daily_positioning_names_a_failed_bhavcopy_fallback(store, client, monkeypatch):
    write_bhav(store)
    monkeypatch.setattr("shunkan.data.chains.get_chain",
                        lambda sym, *a: chain("synthetic (offline demo)", True))
    pos = client.get("/api/analysis/daily/BANKNIFTY").json()["positioning"]
    assert pos["source"] == "synthetic (offline demo)" and pos["is_model"] is True
    assert pos["bhav_error"] == "no BANKNIFTY options expiring after 2026-11-03 in the bhavcopy"


def test_daily_fallback_after_the_close_says_todays_bhavcopy_is_missing(store, client, monkeypatch):
    write_bhav(store)
    monkeypatch.setattr("shunkan.data.chains.get_chain",
                        lambda sym, *a: chain("synthetic (offline demo)", True))
    monkeypatch.setattr("shunkan.markets.now_ist", at(datetime(2026, 11, 4, 19, 0, tzinfo=IST)))
    d = client.get("/api/analysis/daily/NIFTY").json()
    assert d["positioning"]["source"] == BHAV_SRC
    assert d["positioning"]["session_warning"] == \
        "today's bhavcopy not on disk yet; the 2026-11-04 session has closed"
    assert d["session"] == {"today": "2026-11-04", "phase": "post_close", "prev_session": "2026-11-03"}
    monkeypatch.setattr("shunkan.markets.now_ist", at(datetime(2026, 11, 4, 12, 30, tzinfo=IST)))
    d = client.get("/api/analysis/daily/NIFTY").json()
    assert "session_warning" not in d["positioning"] and d["session"]["phase"] == "open"


def test_replay_carries_no_live_session_block(store, client, monkeypatch):
    write_history(store)
    monkeypatch.setattr("shunkan.markets.now_ist", at(datetime(2026, 11, 4, 12, 30, tzinfo=IST)))
    d = client.get("/api/analysis/daily/NIFTY?on=2026-11-03").json()
    assert d["as_of"] == "2026-11-03" and "session" not in d


def test_daily_positioning_keeps_a_kite_chain(store, client, monkeypatch):
    write_bhav(store)
    monkeypatch.setattr("shunkan.data.chains.get_chain",
                        lambda sym, *a: chain("Zerodha Kite (real-time)", False))
    pos = client.get("/api/analysis/daily/NIFTY").json()["positioning"]
    assert pos["source"] == "Zerodha Kite (real-time)" and "fallback_reason" not in pos
    assert pos["pcr_oi"] > 0


def test_daily_chart_drops_an_unsettled_bar(store, client, monkeypatch):
    close = write_history(store, today_bar_captured="2026-11-04T06:30:00+00:00")
    monkeypatch.setattr("shunkan.markets.now_ist", at(datetime(2026, 11, 4, 12, 30, tzinfo=IST)))
    d = client.get("/api/analysis/daily/NIFTY").json()
    assert d["as_of"] == "2026-11-03" and d["chart"]["close"] == pytest.approx(close)
    assert d["chart"]["unsettled_bar_dropped"] == {
        "date": "2026-11-04", "reason": "captured 12:00 IST, before the 15:30 close"}


def test_daily_chart_keeps_a_bar_captured_after_the_close(store, client, monkeypatch):
    write_history(store, today_bar_captured="2026-11-04T10:15:00+00:00")
    monkeypatch.setattr("shunkan.markets.now_ist", at(datetime(2026, 11, 4, 16, 0, tzinfo=IST)))
    d = client.get("/api/analysis/daily/NIFTY").json()
    assert d["as_of"] == "2026-11-04" and d["chart"]["close"] == 24150.0
    assert "unsettled_bar_dropped" not in d["chart"]


@pytest.mark.parametrize("when, offline, skip", [
    (datetime(2026, 11, 4, 17, 0, tzinfo=IST), False, None),
    (datetime(2026, 11, 4, 21, 59, tzinfo=IST), False, None),
    (datetime(2026, 11, 4, 16, 59, tzinfo=IST), False, "outside 17:00-22:00 IST"),
    (datetime(2026, 11, 4, 22, 0, tzinfo=IST), False, "outside 17:00-22:00 IST"),
    (datetime(2026, 11, 7, 18, 0, tzinfo=IST), False, "weekend"),
    (datetime(2026, 11, 4, 18, 0, tzinfo=IST), True, "offline"),
])
def test_facts_refresh_runs_on_weekday_evenings_only(when, offline, skip):
    assert api.facts_refresh_skip(when, offline) == skip


@pytest.mark.parametrize("when, wait", [
    (datetime(2026, 11, 4, 13, 0, tzinfo=IST), 2 * 3600 + 50 * 60),
    (datetime(2026, 11, 4, 15, 35, tzinfo=IST), 15 * 60),
    (datetime(2026, 11, 4, 15, 49, tzinfo=IST), 60),
    (datetime(2026, 11, 4, 15, 50, tzinfo=IST), 6 * 3600),
    (datetime(2026, 11, 4, 16, 0, tzinfo=IST), 6 * 3600),
    (datetime(2026, 11, 6, 16, 0, tzinfo=IST), 6 * 3600),
    (datetime(2026, 11, 7, 12, 0, tzinfo=IST), 6 * 3600),
    (datetime(2026, 11, 9, 10, 0, tzinfo=IST), 5 * 3600 + 50 * 60),
])
def test_history_sync_wakes_for_the_post_close_pass(when, wait):
    assert api.history_sync_sleep(when) == wait


def test_status_reports_the_facts_loop(client):
    st = client.get("/api/status").json()["facts"]
    assert {"runs", "failed", "skipped"} <= set(st)


def test_backfill_counts_never_overwrite_the_loop_failure_count():
    st = {"failed": 4}
    st.update(api._backfill_counts({"fetched": 2, "unpublished": 1, "failed": 3, "days_on_disk": 9}))
    assert st == {"failed": 4, "days_fetched": 2, "days_unpublished": 1, "days_failed": 3,
                  "days_on_disk": 9}
